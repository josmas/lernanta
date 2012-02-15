import logging
import datetime

from django.core.cache import cache
from django.core.validators import MaxLengthValidator
from django.conf import settings
from django.db import models
from django.db.models import Count, Max, Q
from django.template.defaultfilters import slugify
from django.utils.translation import ugettext_lazy as _
from django.template.loader import render_to_string
from django.contrib.sites.models import Site
from django.core.mail import send_mail
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import post_save

from taggit.managers import TaggableManager

from drumbeat import storage
from drumbeat.utils import get_partition_id, safe_filename
from drumbeat.models import ModelBase
from relationships.models import Relationship
from activity.models import Activity, RemoteObject, register_filter
from activity.schema import object_types, verbs
from users.tasks import SendUserEmail
from l10n.models import localize_email
from richtext.models import RichTextField
from content.models import Page
from replies.models import PageComment
from tags.models import GeneralTaggedItem
from tracker import statsd

import caching.base

log = logging.getLogger(__name__)


def determine_image_upload_path(instance, filename):
    return "images/projects/%(partition)d/%(filename)s" % {
        'partition': get_partition_id(instance.pk),
        'filename': safe_filename(filename),
    }


class ProjectManager(caching.base.CachingManager):

    def get_popular(self, limit=0, school=None):
        popular = cache.get('projectspopular')
        if not popular:
            rels = Relationship.objects.filter(deleted=False).values(
                'target_project').annotate(Count('id')).exclude(
                target_project__isnull=True).filter(
                target_project__under_development=False,
                target_project__not_listed=False,
                target_project__archived=False).order_by('-id__count')
            if school:
                rels = rels.filter(target_project__school=school)
            if limit:
                rels = rels[:limit]
            popular = [r['target_project'] for r in rels]
            cache.set('projectspopular', popular, 3000)
        return Project.objects.filter(id__in=popular)

    def get_active(self, limit=0, school=None):
        active = cache.get('projectsactive')
        if not active:
            ct = ContentType.objects.get_for_model(RemoteObject)
            activities = Activity.objects.values('scope_object').annotate(
                Max('created_on')).exclude(scope_object__isnull=True,
                verb=verbs['follow'], target_content_type=ct).filter(
                scope_object__under_development=False,
                scope_object__not_listed=False,
                scope_object__archived=False).order_by('-created_on__max')
            if school:
                activities = activities.filter(
                    scope_object__school=school)
            if limit:
                activities = activities[:limit]
            active = [a['scope_object'] for a in activities]
            cache.set('projectsactive', active, 3000)
        return Project.objects.filter(id__in=active)


class Project(ModelBase):
    """Placeholder model for projects."""
    object_type = object_types['group']

    name = models.CharField(max_length=100)

    # Select kind of project (study group, course, or other)
    STUDY_GROUP = 'study group'
    COURSE = 'course'
    CHALLENGE = 'challenge'
    CATEGORY_CHOICES = (
        (STUDY_GROUP, _('Study Group -- group of people working ' \
                        'collaboratively to acquire and share knowledge.')),
        (COURSE, _('Course -- led by one or more organizers with skills on ' \
                   'a field who direct and help participants during their ' \
                   'learning.')),
        (CHALLENGE, _('Challenge -- series of tasks peers can engage in ' \
                      'to develop skills.'))
    )
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES,
        default=STUDY_GROUP, null=True, blank=False)

    tags = TaggableManager(through=GeneralTaggedItem, blank=True)

    other = models.CharField(max_length=30, blank=True, null=True)
    other_description = models.CharField(max_length=150, blank=True, null=True)

    short_description = models.CharField(max_length=150)
    long_description = RichTextField(validators=[MaxLengthValidator(700)])

    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    school = models.ForeignKey('schools.School', related_name='projects',
        null=True, blank=True)

    detailed_description = models.ForeignKey('content.Page',
        related_name='desc_project', null=True, blank=True)

    image = models.ImageField(upload_to=determine_image_upload_path, null=True,
                              storage=storage.ImageStorage(), blank=True)

    slug = models.SlugField(unique=True, max_length=110)
    featured = models.BooleanField(default=False)
    created_on = models.DateTimeField(
        auto_now_add=True, default=datetime.datetime.now)

    under_development = models.BooleanField(default=True)
    not_listed = models.BooleanField(default=False)
    archived = models.BooleanField(default=False)

    clone_of = models.ForeignKey('projects.Project', blank=True, null=True,
        related_name='derivated_projects')

    imported_from = models.CharField(max_length=150, blank=True, null=True)

    next_projects = models.ManyToManyField('projects.Project',
        symmetrical=False, related_name='previous_projects', blank=True,
        null=True)

    objects = ProjectManager()

    class Meta:
        verbose_name = _('group')

    def __unicode__(self):
        return _('%(name)s %(kind)s') % dict(name=self.name,
            kind=self.kind.lower())

    @models.permalink
    def get_absolute_url(self):
        return ('projects_show', (), {
            'slug': self.slug,
        })

    def friendly_verb(self, verb):
        if verbs['post'] == verb:
            return _('created')

    @property
    def kind(self):
        return self.other.lower() if self.other else self.category

    def followers(self, include_deleted=False):
        relationships = Relationship.objects.all()
        if not include_deleted:
            relationships = relationships.filter(
                source__deleted=False)
        return relationships.filter(target_project=self,
            deleted=False)

    def previous_followers(self, include_deleted=False):
        """Return a list of users who were followers if this project."""
        relationships = Relationship.objects.all()
        if not include_deleted:
            relationships = relationships.filter(
                 source__deleted=False)
        return relationships.filter(target_project=self,
            deleted=True)

    def non_participant_followers(self, include_deleted=False):
        return self.followers(include_deleted).exclude(
            source__id__in=self.participants(include_deleted).values('user_id'))

    def participants(self, include_deleted=False):
        """Return a list of users participating in this project."""
        participations = Participation.objects.all()
        if not include_deleted:
            participations = participations.filter(user__deleted=False)
        return participations.filter(project=self,
            left_on__isnull=True)

    def non_organizer_participants(self, include_deleted=False):
        return self.participants(include_deleted).filter(organizing=False)

    def adopters(self, include_deleted=False):
        return self.participants(include_deleted).filter(Q(adopter=True) | Q(organizing=True))

    def non_adopter_participants(self, include_deleted=False):
        return self.non_organizer_participants(include_deleted).filter(
            adopter=False)

    def organizers(self, include_deleted=False):
        return self.participants(include_deleted).filter(organizing=True)

    def is_organizing(self, user):
        if user.is_authenticated():
            profile = user.get_profile()
            is_organizer = self.organizers().filter(user=profile).exists()
            is_superuser = user.is_superuser
            return is_organizer or is_superuser
        else:
            return False

    def is_following(self, user):
        if user.is_authenticated():
            profile = user.get_profile()
            is_following = self.followers().filter(source=profile).exists()
            return is_following
        else:
            return False

    def is_participating(self, user):
        if user.is_authenticated():
            profile = user.get_profile()
            is_organizer_or_participant = self.participants().filter(
                user=profile).exists()
            is_superuser = user.is_superuser
            return is_organizer_or_participant or is_superuser
        else:
            return False

    def get_metrics_permissions(self, user):
        """Provides metrics related permissions for metrics overview
        and csv download."""
        if user.is_authenticated():
            if user.is_superuser:
                return True, True
            allowed_schools = settings.STATISTICS_ENABLED_SCHOOLS
            if not self.school or self.school.slug not in allowed_schools:
                return False, False
            csv_downloaders = settings.STATISTICS_CSV_DOWNLOADERS
            profile = user.get_profile()
            csv_permission = profile.username in csv_downloaders
            is_school_organizer = self.school.organizers.filter(
                id=user.id).exists()
            if is_school_organizer or self.is_organizing(user):
                return True, csv_permission
        return False, False

    def activities(self):
        return Activity.objects.filter(deleted=False,
            scope_object=self).order_by('-created_on')

    def create(self):
        self.save()
        self.send_creation_notification()

    def save(self):
        """Make sure each project has a unique slug."""
        count = 1
        if not self.slug:
            slug = slugify(self.name)
            self.slug = slug
            while True:
                existing = Project.objects.filter(slug=self.slug)
                if len(existing) == 0:
                    break
                self.slug = "%s-%s" % (slug, count + 1)
                count += 1
        super(Project, self).save()

    def get_image_url(self):
        missing = settings.MEDIA_URL + 'images/project-missing.png'
        image_path = self.image.url if self.image else missing
        return image_path

    def send_creation_notification(self):
        """Send notification when a new project is created."""
        context = {
            'project': self,
            'domain': Site.objects.get_current().domain,
        }
        subjects, bodies = localize_email(
            'projects/emails/project_created_subject.txt',
            'projects/emails/project_created.txt', context)
        for organizer in self.organizers():
            SendUserEmail.apply_async((organizer.user, subjects, bodies))
        admin_subject = render_to_string(
            "projects/emails/admin_project_created_subject.txt",
            context).strip()
        admin_body = render_to_string(
            "projects/emails/admin_project_created.txt", context).strip()
        for admin_email in settings.ADMIN_PROJECT_CREATE_EMAIL:
            send_mail(admin_subject, admin_body, admin_email,
                [admin_email], fail_silently=True)

    def accepted_school(self):
        # Used previously when schools had to decline groups.
        return self.school

    def check_tasks_completion(self, user):
        total_count = self.pages.filter(listed=True,
            deleted=False).count()
        completed_count = PerUserTaskCompletion.objects.filter(
            page__project=self, page__deleted=False,
            unchecked_on__isnull=True, user=user).count()
        if total_count == completed_count:
            badges = self.get_project_badges(only_self_completion=True)
            for badge in badges:
                badge.award_to(user)

    def completed_tasks_users(self):
        total_count = self.pages.filter(listed=True,
            deleted=False).count()
        completed_stats = PerUserTaskCompletion.objects.filter(
            page__project=self, page__deleted=False,
            unchecked_on__isnull=True).values(
            'user__username').annotate(completed_count=Count('page')).filter(
            completed_count=total_count)
        usernames = completed_stats.values(
            'user__username')
        return Relationship.objects.filter(source__username__in=usernames,
            target_project=self, source__deleted=False)

    def get_project_badges(self, only_self_completion=False,
            only_peer_skill=False, only_peer_community=False):
        from badges.models import Badge
        assessment_types = []
        badge_types = []
        if not only_self_completion and not only_peer_community:
            assessment_types.append(Badge.PEER)
            badge_types.append(Badge.SKILL)
        if not only_peer_skill and not only_peer_community:
            assessment_types.append(Badge.SELF)
            badge_types.append(Badge.COMPLETION)
        if not only_peer_skill and not only_self_completion:
            assessment_types.append(Badge.PEER)
            badge_types.append(Badge.COMMUNITY)
        if assessment_types and badge_types:
            return self.badges.filter(assessment_type__in=assessment_types,
                badge_type__in=badge_types)
        else:
            return Badge.objects.none()

    def get_upon_completion_badges(self, user):
        from badges.models import Badge, Award
        if user.is_authenticated():
            profile = user.get_profile()
            awarded_badges = Award.objects.filter(
                user=profile).values('badge_id')
            self_completion_badges = self.get_project_badges(
                only_self_completion=True)
            upon_completion_badges = []
            for badge in self_completion_badges:
                missing_prerequisites = badge.prerequisites.exclude(
                    id__in=awarded_badges).exclude(
                    id__in=self_completion_badges.values('id'))
                if not missing_prerequisites.exists():
                    upon_completion_badges.append(badge.id)
            return Badge.objects.filter(id__in=upon_completion_badges)
        else:
            return Badge.objects.none()

    def get_awarded_badges(self, user, only_peer_skill=False):
        from badges.models import Badge, Award
        if user.is_authenticated():
            profile = user.get_profile()
            awarded_badges = Award.objects.filter(
                user=profile).values('badge_id')
            project_badges = self.get_project_badges(only_peer_skill)
            return project_badges.filter(
                id__in=awarded_badges)
        else:
            return Badge.objects.none()

    def get_badges_in_progress(self, user):
        from badges.models import Badge, Award, Submission
        if user.is_authenticated():
            profile = user.get_profile()
            awarded_badges = Award.objects.filter(
                user=profile).values('badge_id')
            attempted_badges = Submission.objects.filter(
                author=profile).values('badge_id')
            project_badges = self.get_project_badges(
                only_peer_skill=True)
            return project_badges.filter(
                id__in=attempted_badges).exclude(
                id__in=awarded_badges)
        else:
            return Badge.objects.none()

    def get_non_attempted_badges(self, user):
        from badges.models import Badge, Award, Submission
        if user.is_authenticated():
            profile = user.get_profile()
            awarded_badges = Award.objects.filter(
                user=profile).values('badge_id')
            attempted_badges = Submission.objects.filter(
                author=profile).values('badge_id')
            project_badges = self.get_project_badges(
                only_peer_skill=True)
            # Excluding both awarded and attempted badges
            # In case honorary award do not rely on submissions.
            return project_badges.exclude(
                id__in=attempted_badges).exclude(
                id__in=awarded_badges)
        else:
            return Badge.objects.none()

    def get_need_reviews_badges(self, user):
        from badges.models import Badge, Award, Submission
        if user.is_authenticated():
            profile = user.get_profile()
            project_badges = self.get_project_badges(
                only_peer_skill=True)
            peers_submissions = Submission.objects.filter(
                badge__id__in=project_badges.values('id')).exclude(
                author=profile)
            peers_attempted_badges = project_badges.filter(
                id__in=peers_submissions.values('badge_id'))
            need_reviews_badges = []
            for badge in peers_attempted_badges:
                peers_awards = Award.objects.filter(
                    badge=badge).exclude(user=profile)
                pending_submissions = peers_submissions.filter(
                    badge=badge).exclude(
                    author__id__in=peers_awards.values('user_id'))
                if pending_submissions.exists():
                    need_reviews_badges.append(badge.id)
            return project_badges.filter(
                id__in=need_reviews_badges)
        else:
            return Badge.objects.none()

    def get_non_started_next_projects(self, user):
        """To be displayed in the Join Next Challenges section."""
        if user.is_authenticated():
            profile = user.get_profile()
            joined = Participation.objects.filter(
                user=profile).values('project_id')
            return self.next_projects.exclude(
                id__in=joined)
        else:
            return Project.objects.none()

    @staticmethod
    def filter_activities(activities):
        from statuses.models import Status
        content_types = [
            ContentType.objects.get_for_model(Page),
            ContentType.objects.get_for_model(PageComment),
            ContentType.objects.get_for_model(Status),
            ContentType.objects.get_for_model(Project),
        ]
        return activities.filter(target_content_type__in=content_types)

    @staticmethod
    def filter_learning_activities(activities):
        pages_ct = ContentType.objects.get_for_model(Page)
        comments_ct = ContentType.objects.get_for_model(PageComment)
        return activities.filter(
            target_content_type__in=[pages_ct, comments_ct])

register_filter('default', Project.filter_activities)
register_filter('learning', Project.filter_learning_activities)


class Participation(ModelBase):
    user = models.ForeignKey('users.UserProfile',
        related_name='participations')
    project = models.ForeignKey('projects.Project',
        related_name='participations')
    organizing = models.BooleanField(default=False)
    adopter = models.BooleanField(default=False)
    joined_on = models.DateTimeField(
        auto_now_add=True, default=datetime.datetime.now)
    left_on = models.DateTimeField(blank=True, null=True)
    # Notification Preferences.
    no_organizers_wall_updates = models.BooleanField(default=False)
    no_organizers_content_updates = models.BooleanField(default=False)
    no_participants_wall_updates = models.BooleanField(default=False)
    no_participants_content_updates = models.BooleanField(default=False)


class PerUserTaskCompletion(ModelBase):
    user = models.ForeignKey('users.UserProfile',
        related_name='peruser_task_completion')
    page = models.ForeignKey('content.Page',
        related_name='peruser_task_completion')
    checked_on = models.DateTimeField(auto_now_add=True,
        default=datetime.datetime.now)
    unchecked_on = models.DateTimeField(blank=True, null=True)
    url = models.URLField(max_length=1023, blank=True, null=True)


###########
# Signals #
###########

def check_tasks_completion(sender, **kwargs):
    instance = kwargs.get('instance', None)
    if isinstance(instance, PerUserTaskCompletion):
        project = instance.page.project
        user = instance.user
        project.check_tasks_completion(user)


post_save.connect(check_tasks_completion, sender=PerUserTaskCompletion,
    dispatch_uid='projects_check_tasks_completion')


def post_save_project(sender, **kwargs):
    instance = kwargs.get('instance', None)
    created = kwargs.get('created', False)
    is_project = isinstance(instance, Project)
    if created and is_project:
        statsd.Statsd.increment('groups')


post_save.connect(post_save_project, sender=Project,
    dispatch_uid='projects_post_save_project')


def post_save_participation(sender, **kwargs):
    instance = kwargs.get('instance', None)
    created = kwargs.get('created', False)
    is_participation = isinstance(instance, Participation)
    if created and is_participation:
        statsd.Statsd.increment('joins')


post_save.connect(post_save_participation, sender=Participation,
    dispatch_uid='projects_post_save_participation')
