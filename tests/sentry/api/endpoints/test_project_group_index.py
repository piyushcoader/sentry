from __future__ import absolute_import

import json
from datetime import timedelta
from uuid import uuid4

import six
from django.utils import timezone
from exam import fixture
from mock import patch

from sentry import tagstore
from sentry.models import (
    Activity, ApiToken, EventMapping, Group, GroupAssignee, GroupBookmark, GroupHash, GroupResolution,
    GroupSeen, GroupSnooze, GroupStatus, GroupSubscription,
    GroupTombstone, Release, UserOption, GroupShare,
)
from sentry.models.event import Event
from sentry.testutils import APITestCase
from sentry.testutils.helpers import parse_link_header
from six.moves.urllib.parse import quote


class GroupListTest(APITestCase):
    def _parse_links(self, header):
        # links come in {url: {...attrs}}, but we need {rel: {...attrs}}
        links = {}
        for url, attrs in six.iteritems(parse_link_header(header)):
            links[attrs['rel']] = attrs
            attrs['href'] = url
        return links

    @fixture
    def path(self):
        return '/api/0/projects/{}/{}/issues/'.format(
            self.project.organization.slug,
            self.project.slug,
        )

    def test_sort_by_date_with_tag(self):
        # XXX(dcramer): this tests a case where an ambiguous column name existed
        now = timezone.now()
        group1 = self.create_group(
            checksum='a' * 32,
            last_seen=now - timedelta(seconds=1),
        )
        self.login_as(user=self.user)

        response = self.client.get(
            '{}?sort_by=date&query=is:unresolved'.format(self.path),
            format='json',
        )
        assert response.status_code == 200
        assert len(response.data) == 1
        assert response.data[0]['id'] == six.text_type(group1.id)

    def test_simple_pagination(self):
        now = timezone.now().replace(microsecond=0)
        group1 = self.create_group(
            checksum='a' * 32,
            last_seen=now - timedelta(seconds=1),
        )
        group2 = self.create_group(
            checksum='b' * 32,
            last_seen=now,
        )
        # group3 = self.create_group(
        #     checksum='c' * 32,
        #     last_seen=now - timedelta(seconds=1),
        # )

        self.login_as(user=self.user)
        response = self.client.get(
            '{}?sort_by=date&limit=1'.format(self.path),
            format='json',
        )
        assert response.status_code == 200
        assert len(response.data) == 1
        assert response.data[0]['id'] == six.text_type(group2.id)

        links = self._parse_links(response['Link'])

        assert links['previous']['results'] == 'false'
        assert links['next']['results'] == 'true'

        response = self.client.get(links['next']['href'], format='json')
        assert response.status_code == 200
        assert len(response.data) == 1
        assert response.data[0]['id'] == six.text_type(group1.id)

        links = self._parse_links(response['Link'])

        assert links['previous']['results'] == 'true'
        assert links['next']['results'] == 'false'

        # TODO(dcramer): not working correctly
        # print(links['previous']['cursor'])
        # response = self.client.get(links['previous']['href'], format='json')
        # assert response.status_code == 200
        # assert len(response.data) == 1
        # assert response.data[0]['id'] == six.text_type(group2.id)

        # links = self._parse_links(response['Link'])

        # assert links['previous']['results'] == 'false'
        # assert links['next']['results'] == 'true'

        # print(links['previous']['cursor'])
        # response = self.client.get(links['previous']['href'], format='json')
        # assert response.status_code == 200
        # assert len(response.data) == 0

        # group3 = self.create_group(
        #     checksum='c' * 32,
        #     last_seen=now + timedelta(seconds=1),
        # )

        # links = self._parse_links(response['Link'])

        # assert links['previous']['results'] == 'false'
        # assert links['next']['results'] == 'true'

        # print(links['previous']['cursor'])
        # response = self.client.get(links['previous']['href'], format='json')
        # assert response.status_code == 200
        # assert len(response.data) == 1
        # assert response.data[0]['id'] == six.text_type(group3.id)

    def test_stats_period(self):
        # TODO(dcramer): this test really only checks if validation happens
        # on statsPeriod
        now = timezone.now()
        self.create_group(
            checksum='a' * 32,
            last_seen=now - timedelta(seconds=1),
        )
        self.create_group(
            checksum='b' * 32,
            last_seen=now,
        )

        self.login_as(user=self.user)

        response = self.client.get('{}?statsPeriod=24h'.format(self.path), format='json')
        assert response.status_code == 200

        response = self.client.get('{}?statsPeriod=14d'.format(self.path), format='json')
        assert response.status_code == 200

        response = self.client.get('{}?statsPeriod='.format(self.path), format='json')
        assert response.status_code == 200

        response = self.client.get('{}?statsPeriod=48h'.format(self.path), format='json')
        assert response.status_code == 400

    def test_environment(self):
        self.create_event(tags={'environment': 'production'})

        self.login_as(user=self.user)

        response = self.client.get(self.path + '?environment=production', format='json')
        assert response.status_code == 200

        response = self.client.get(self.path + '?environment=garbage', format='json')
        assert response.status_code == 200

    def test_auto_resolved(self):
        project = self.project
        project.update_option('sentry:resolve_age', 1)
        now = timezone.now()
        self.create_group(
            checksum='a' * 32,
            last_seen=now - timedelta(days=1),
        )
        group2 = self.create_group(
            checksum='b' * 32,
            last_seen=now,
        )

        self.login_as(user=self.user)
        response = self.client.get(self.path, format='json')
        assert response.status_code == 200
        assert len(response.data) == 1
        assert response.data[0]['id'] == six.text_type(group2.id)

    def test_lookup_by_event_id(self):
        project = self.project
        project.update_option('sentry:resolve_age', 1)
        group = self.create_group(checksum='a' * 32)
        self.create_group(checksum='b' * 32)
        event_id = 'c' * 32
        event = Event.objects.create(project_id=self.project.id, event_id=event_id)
        EventMapping.objects.create(
            event_id=event_id,
            project=group.project,
            group=group,
        )

        self.login_as(user=self.user)
        response = self.client.get('{}?query={}'.format(self.path, 'c' * 32), format='json')
        assert response.status_code == 200
        assert len(response.data) == 1
        assert response.data[0]['id'] == six.text_type(group.id)
        assert response.data[0]['matchingEventId'] == event.id

    def test_lookup_by_event_id_with_whitespace(self):
        project = self.project
        project.update_option('sentry:resolve_age', 1)
        group = self.create_group(checksum='a' * 32)
        self.create_group(checksum='b' * 32)
        EventMapping.objects.create(
            event_id='c' * 32,
            project=group.project,
            group=group,
        )

        self.login_as(user=self.user)
        response = self.client.get(
            '{}?query=%20%20{}%20%20'.format(self.path, 'c' * 32), format='json'
        )
        assert response.status_code == 200
        assert len(response.data) == 1
        assert response.data[0]['id'] == six.text_type(group.id)

    def test_lookup_by_unknown_event_id(self):
        project = self.project
        project.update_option('sentry:resolve_age', 1)
        self.create_group(checksum='a' * 32)
        self.create_group(checksum='b' * 32)

        self.login_as(user=self.user)
        response = self.client.get('{}?query={}'.format(self.path, 'c' * 32), format='json')
        assert response.status_code == 200
        assert len(response.data) == 0

    def test_lookup_by_first_release(self):
        self.login_as(self.user)
        project = self.project
        project2 = self.create_project(name='baz', organization=project.organization)
        release = Release.objects.create(organization=project.organization, version='12345')
        release.add_project(project)
        release.add_project(project2)
        group = self.create_group(checksum='a' * 32, project=project, first_release=release)
        self.create_group(checksum='b' * 32, project=project2, first_release=release)
        url = '%s?query=%s' % (self.path, quote('first-release:"%s"' % release.version))
        response = self.client.get(url, format='json')
        issues = json.loads(response.content)
        assert response.status_code == 200
        assert len(issues) == 1
        assert int(issues[0]['id']) == group.id

    def test_lookup_by_release(self):
        self.login_as(self.user)
        project = self.project
        project2 = self.create_project(name='baz', organization=project.organization)
        release = Release.objects.create(organization=project.organization, version='12345')
        release.add_project(project)
        release.add_project(project2)
        group = self.create_group(checksum='a' * 32, project=project)
        group2 = self.create_group(checksum='b' * 32, project=project2)
        tagstore.create_group_tag_value(
            project_id=project.id, group_id=group.id, environment_id=None,
            key='sentry:release', value=release.version
        )

        tagstore.create_group_tag_value(
            project_id=project2.id, group_id=group2.id, environment_id=None,
            key='sentry:release', value=release.version
        )

        url = '%s?query=%s' % (self.path, quote('release:"%s"' % release.version))
        response = self.client.get(url, format='json')
        issues = json.loads(response.content)
        assert response.status_code == 200
        assert len(issues) == 1
        assert int(issues[0]['id']) == group.id

    def test_pending_delete_pending_merge_excluded(self):
        self.create_group(
            checksum='a' * 32,
            status=GroupStatus.PENDING_DELETION,
        )
        group = self.create_group(
            checksum='b' * 32,
        )
        self.create_group(
            checksum='c' * 32,
            status=GroupStatus.DELETION_IN_PROGRESS,
        )
        self.create_group(
            checksum='d' * 32,
            status=GroupStatus.PENDING_MERGE,
        )

        self.login_as(user=self.user)

        response = self.client.get(self.path, format='json')
        assert len(response.data) == 1
        assert response.data[0]['id'] == six.text_type(group.id)

    def test_filters_based_on_retention(self):
        self.login_as(user=self.user)

        self.create_group(last_seen=timezone.now() - timedelta(days=2))

        with self.options({'system.event-retention-days': 1}):
            response = self.client.get(self.path)

        assert response.status_code == 200, response.content
        assert len(response.data) == 0

    def test_token_auth(self):
        token = ApiToken.objects.create(user=self.user, scopes=256)
        response = self.client.get(
            self.path,
            format='json',
            HTTP_AUTHORIZATION='Bearer %s' %
            token.token)
        assert response.status_code == 200, response.content


class GroupUpdateTest(APITestCase):
    @fixture
    def path(self):
        return '/api/0/projects/{}/{}/issues/'.format(
            self.project.organization.slug,
            self.project.slug,
        )

    def assertNoResolution(self, group):
        assert not GroupResolution.objects.filter(
            group=group,
        ).exists()

    def test_global_resolve(self):
        group1 = self.create_group(checksum='a' * 32, status=GroupStatus.RESOLVED)
        group2 = self.create_group(checksum='b' * 32, status=GroupStatus.UNRESOLVED)
        group3 = self.create_group(checksum='c' * 32, status=GroupStatus.IGNORED)
        group4 = self.create_group(
            project=self.create_project(slug='foo'),
            checksum='b' * 32,
            status=GroupStatus.UNRESOLVED
        )

        self.login_as(user=self.user)
        response = self.client.put(
            '{}?status=unresolved'.format(self.path),
            data={
                'status': 'resolved',
            },
            format='json',
        )
        assert response.status_code == 200, response.data
        assert response.data == {
            'status': 'resolved',
            'statusDetails': {},
        }

        # the previously resolved entry should not be included
        new_group1 = Group.objects.get(id=group1.id)
        assert new_group1.status == GroupStatus.RESOLVED
        assert new_group1.resolved_at is None

        # this wont exist because it wasn't affected
        assert not GroupSubscription.objects.filter(
            user=self.user,
            group=new_group1,
        ).exists()

        new_group2 = Group.objects.get(id=group2.id)
        assert new_group2.status == GroupStatus.RESOLVED
        assert new_group2.resolved_at is not None

        assert GroupSubscription.objects.filter(
            user=self.user,
            group=new_group2,
            is_active=True,
        ).exists()

        # the ignored entry should not be included
        new_group3 = Group.objects.get(id=group3.id)
        assert new_group3.status == GroupStatus.IGNORED
        assert new_group3.resolved_at is None

        assert not GroupSubscription.objects.filter(
            user=self.user,
            group=new_group3,
        )

        new_group4 = Group.objects.get(id=group4.id)
        assert new_group4.status == GroupStatus.UNRESOLVED
        assert new_group4.resolved_at is None

        assert not GroupSubscription.objects.filter(
            user=self.user,
            group=new_group4,
        )

    def test_bulk_resolve(self):
        self.login_as(user=self.user)

        for i in range(200):
            self.create_group(status=GroupStatus.UNRESOLVED)

        response = self.client.get(
            '{}?sort_by=date&query=is:unresolved'.format(self.path),
            format='json',
        )

        assert len(response.data) == 100

        response = self.client.put(
            '{}?status=unresolved'.format(self.path),
            data={
                'status': 'resolved',
            },
            format='json',
        )
        assert response.status_code == 200, response.data

        assert response.data == {
            'status': 'resolved',
            'statusDetails': {},
        }
        response = self.client.get(
            '{}?sort_by=date&query=is:unresolved'.format(self.path),
            format='json',
        )

        assert len(response.data) == 0

    def test_self_assign_issue(self):
        group = self.create_group(checksum='b' * 32, status=GroupStatus.UNRESOLVED)
        user = self.user

        uo1 = UserOption.objects.create(key='self_assign_issue', value='1', project=None, user=user)

        self.login_as(user=user)
        url = '{url}?id={group.id}'.format(url=self.path, group=group)
        response = self.client.put(
            url,
            data={
                'status': 'resolved',
            },
            format='json',
        )

        assert response.status_code == 200, response.data
        assert response.data['assignedTo']['id'] == six.text_type(user.id)
        assert response.data['assignedTo']['type'] == 'user'
        assert response.data['status'] == 'resolved'

        assert GroupAssignee.objects.filter(group=group, user=user).exists()

        assert GroupSubscription.objects.filter(
            user=user,
            group=group,
            is_active=True,
        ).exists()

        uo1.delete()

    def test_self_assign_issue_next_release(self):
        release = Release.objects.create(organization_id=self.project.organization_id, version='a')
        release.add_project(self.project)

        group = self.create_group(
            checksum='a' * 32,
            status=GroupStatus.UNRESOLVED,
        )

        uo1 = UserOption.objects.create(
            key='self_assign_issue', value='1', project=None, user=self.user
        )

        self.login_as(user=self.user)

        url = '{url}?id={group.id}'.format(
            url=self.path,
            group=group,
        )
        response = self.client.put(
            url, data={
                'status': 'resolvedInNextRelease',
            }, format='json'
        )
        assert response.status_code == 200
        assert response.data['status'] == 'resolved'
        assert response.data['statusDetails']['inNextRelease']
        assert response.data['assignedTo']['id'] == six.text_type(self.user.id)
        assert response.data['assignedTo']['type'] == 'user'

        group = Group.objects.get(id=group.id)
        assert group.status == GroupStatus.RESOLVED

        assert GroupResolution.objects.filter(
            group=group,
            release=release,
        ).exists()

        assert GroupSubscription.objects.filter(
            user=self.user,
            group=group,
            is_active=True,
        ).exists()

        activity = Activity.objects.get(
            group=group,
            type=Activity.SET_RESOLVED_IN_RELEASE,
        )
        assert activity.data['version'] == ''
        uo1.delete()

    def test_selective_status_update(self):
        group1 = self.create_group(checksum='a' * 32, status=GroupStatus.RESOLVED)
        group2 = self.create_group(checksum='b' * 32, status=GroupStatus.UNRESOLVED)
        group3 = self.create_group(checksum='c' * 32, status=GroupStatus.IGNORED)
        group4 = self.create_group(
            project=self.create_project(slug='foo'),
            checksum='b' * 32,
            status=GroupStatus.UNRESOLVED
        )

        self.login_as(user=self.user)
        url = '{url}?id={group1.id}&id={group2.id}&group4={group4.id}'.format(
            url=self.path,
            group1=group1,
            group2=group2,
            group4=group4,
        )
        response = self.client.put(
            url, data={
                'status': 'resolved',
            }, format='json'
        )
        assert response.status_code == 200
        assert response.data == {
            'status': 'resolved',
            'statusDetails': {},
        }

        new_group1 = Group.objects.get(id=group1.id)
        assert new_group1.resolved_at is not None
        assert new_group1.status == GroupStatus.RESOLVED

        new_group2 = Group.objects.get(id=group2.id)
        assert new_group2.resolved_at is not None
        assert new_group2.status == GroupStatus.RESOLVED

        assert GroupSubscription.objects.filter(
            user=self.user,
            group=new_group2,
            is_active=True,
        ).exists()

        new_group3 = Group.objects.get(id=group3.id)
        assert new_group3.resolved_at is None
        assert new_group3.status == GroupStatus.IGNORED

        new_group4 = Group.objects.get(id=group4.id)
        assert new_group4.resolved_at is None
        assert new_group4.status == GroupStatus.UNRESOLVED

    def test_set_resolved_in_current_release(self):
        release = Release.objects.create(organization_id=self.project.organization_id, version='a')
        release.add_project(self.project)

        group = self.create_group(
            checksum='a' * 32,
            status=GroupStatus.UNRESOLVED,
        )

        self.login_as(user=self.user)

        url = '{url}?id={group.id}'.format(
            url=self.path,
            group=group,
        )
        response = self.client.put(
            url,
            data={
                'status': 'resolved',
                'statusDetails': {
                    'inRelease': 'latest',
                },
            },
            format='json'
        )
        assert response.status_code == 200
        assert response.data['status'] == 'resolved'
        assert response.data['statusDetails']['inRelease'] == release.version
        assert response.data['statusDetails']['actor']['id'] == six.text_type(self.user.id)

        group = Group.objects.get(id=group.id)
        assert group.status == GroupStatus.RESOLVED

        resolution = GroupResolution.objects.get(
            group=group,
        )
        assert resolution.release == release
        assert resolution.type == GroupResolution.Type.in_release
        assert resolution.status == GroupResolution.Status.resolved
        assert resolution.actor_id == self.user.id

        assert GroupSubscription.objects.filter(
            user=self.user,
            group=group,
            is_active=True,
        ).exists()

        activity = Activity.objects.get(
            group=group,
            type=Activity.SET_RESOLVED_IN_RELEASE,
        )
        assert activity.data['version'] == release.version

    def test_set_resolved_in_explicit_release(self):
        release = Release.objects.create(organization_id=self.project.organization_id, version='a')
        release.add_project(self.project)
        release2 = Release.objects.create(organization_id=self.project.organization_id, version='b')
        release2.add_project(self.project)

        group = self.create_group(
            checksum='a' * 32,
            status=GroupStatus.UNRESOLVED,
        )

        self.login_as(user=self.user)

        url = '{url}?id={group.id}'.format(
            url=self.path,
            group=group,
        )
        response = self.client.put(
            url,
            data={
                'status': 'resolved',
                'statusDetails': {
                    'inRelease': release.version,
                },
            },
            format='json'
        )
        assert response.status_code == 200
        assert response.data['status'] == 'resolved'
        assert response.data['statusDetails']['inRelease'] == release.version
        assert response.data['statusDetails']['actor']['id'] == six.text_type(self.user.id)

        group = Group.objects.get(id=group.id)
        assert group.status == GroupStatus.RESOLVED

        resolution = GroupResolution.objects.get(
            group=group,
        )
        assert resolution.release == release
        assert resolution.type == GroupResolution.Type.in_release
        assert resolution.status == GroupResolution.Status.resolved
        assert resolution.actor_id == self.user.id

        assert GroupSubscription.objects.filter(
            user=self.user,
            group=group,
            is_active=True,
        ).exists()

        activity = Activity.objects.get(
            group=group,
            type=Activity.SET_RESOLVED_IN_RELEASE,
        )
        assert activity.data['version'] == release.version

    def test_set_resolved_in_next_release(self):
        release = Release.objects.create(organization_id=self.project.organization_id, version='a')
        release.add_project(self.project)

        group = self.create_group(
            checksum='a' * 32,
            status=GroupStatus.UNRESOLVED,
        )

        self.login_as(user=self.user)

        url = '{url}?id={group.id}'.format(
            url=self.path,
            group=group,
        )
        response = self.client.put(
            url,
            data={
                'status': 'resolved',
                'statusDetails': {
                    'inNextRelease': True,
                },
            },
            format='json'
        )
        assert response.status_code == 200
        assert response.data['status'] == 'resolved'
        assert response.data['statusDetails']['inNextRelease']
        assert response.data['statusDetails']['actor']['id'] == six.text_type(self.user.id)

        group = Group.objects.get(id=group.id)
        assert group.status == GroupStatus.RESOLVED

        resolution = GroupResolution.objects.get(
            group=group,
        )
        assert resolution.release == release
        assert resolution.type == GroupResolution.Type.in_next_release
        assert resolution.status == GroupResolution.Status.pending
        assert resolution.actor_id == self.user.id

        assert GroupSubscription.objects.filter(
            user=self.user,
            group=group,
            is_active=True,
        ).exists()

        activity = Activity.objects.get(
            group=group,
            type=Activity.SET_RESOLVED_IN_RELEASE,
        )
        assert activity.data['version'] == ''

    def test_set_resolved_in_next_release_legacy(self):
        release = Release.objects.create(organization_id=self.project.organization_id, version='a')
        release.add_project(self.project)

        group = self.create_group(
            checksum='a' * 32,
            status=GroupStatus.UNRESOLVED,
        )

        self.login_as(user=self.user)

        url = '{url}?id={group.id}'.format(
            url=self.path,
            group=group,
        )
        response = self.client.put(
            url, data={
                'status': 'resolvedInNextRelease',
            }, format='json'
        )
        assert response.status_code == 200
        assert response.data['status'] == 'resolved'
        assert response.data['statusDetails']['inNextRelease']
        assert response.data['statusDetails']['actor']['id'] == six.text_type(self.user.id)

        group = Group.objects.get(id=group.id)
        assert group.status == GroupStatus.RESOLVED

        resolution = GroupResolution.objects.get(
            group=group,
        )
        assert resolution.release == release
        assert resolution.type == GroupResolution.Type.in_next_release
        assert resolution.status == GroupResolution.Status.pending
        assert resolution.actor_id == self.user.id

        assert GroupSubscription.objects.filter(
            user=self.user,
            group=group,
            is_active=True,
        ).exists()

        activity = Activity.objects.get(
            group=group,
            type=Activity.SET_RESOLVED_IN_RELEASE,
        )
        assert activity.data['version'] == ''

    def test_set_unresolved(self):
        release = self.create_release(project=self.project, version='abc')
        group = self.create_group(checksum='a' * 32, status=GroupStatus.RESOLVED)
        GroupResolution.objects.create(
            group=group,
            release=release,
        )

        self.login_as(user=self.user)

        url = '{url}?id={group.id}'.format(
            url=self.path,
            group=group,
        )
        response = self.client.put(
            url, data={
                'status': 'unresolved',
            }, format='json'
        )
        assert response.status_code == 200
        assert response.data == {
            'status': 'unresolved',
            'statusDetails': {},
        }

        group = Group.objects.get(id=group.id)
        assert group.status == GroupStatus.UNRESOLVED

        self.assertNoResolution(group)

        assert GroupSubscription.objects.filter(
            user=self.user,
            group=group,
            is_active=True,
        ).exists()

    def test_set_unresolved_on_snooze(self):
        group = self.create_group(checksum='a' * 32, status=GroupStatus.IGNORED)

        GroupSnooze.objects.create(
            group=group,
            until=timezone.now() - timedelta(days=1),
        )

        self.login_as(user=self.user)

        url = '{url}?id={group.id}'.format(
            url=self.path,
            group=group,
        )
        response = self.client.put(
            url, data={
                'status': 'unresolved',
            }, format='json'
        )
        assert response.status_code == 200
        assert response.data == {
            'status': 'unresolved',
            'statusDetails': {},
        }

        group = Group.objects.get(id=group.id)
        assert group.status == GroupStatus.UNRESOLVED

    def test_basic_ignore(self):
        group = self.create_group(checksum='a' * 32, status=GroupStatus.RESOLVED)

        snooze = GroupSnooze.objects.create(
            group=group,
            until=timezone.now(),
        )

        self.login_as(user=self.user)

        url = '{url}?id={group.id}'.format(
            url=self.path,
            group=group,
        )
        response = self.client.put(
            url, data={
                'status': 'ignored',
            }, format='json'
        )

        assert response.status_code == 200

        # existing snooze objects should be cleaned up
        assert not GroupSnooze.objects.filter(id=snooze.id).exists()

        group = Group.objects.get(id=group.id)
        assert group.status == GroupStatus.IGNORED

        assert response.data == {
            'status': 'ignored',
            'statusDetails': {},
        }

    def test_snooze_duration(self):
        group = self.create_group(checksum='a' * 32, status=GroupStatus.RESOLVED)

        self.login_as(user=self.user)

        url = '{url}?id={group.id}'.format(
            url=self.path,
            group=group,
        )
        response = self.client.put(
            url, data={
                'status': 'ignored',
                'ignoreDuration': 30,
            }, format='json'
        )

        assert response.status_code == 200

        snooze = GroupSnooze.objects.get(group=group)
        snooze.until = snooze.until.replace(microsecond=0)

        # Drop microsecond value for MySQL
        now = timezone.now().replace(microsecond=0)

        assert snooze.count is None
        assert snooze.until > now + timedelta(minutes=29)
        assert snooze.until < now + timedelta(minutes=31)
        assert snooze.user_count is None
        assert snooze.user_window is None
        assert snooze.window is None

        # Drop microsecond value for MySQL
        response.data['statusDetails']['ignoreUntil'] = response.data['statusDetails'
                                                                      ]['ignoreUntil'].replace(
                                                                          microsecond=0
                                                                      )  # noqa

        assert response.data['status'] == 'ignored'
        assert response.data['statusDetails']['ignoreCount'] == snooze.count
        assert response.data['statusDetails']['ignoreWindow'] == snooze.window
        assert response.data['statusDetails']['ignoreUserCount'] == snooze.user_count
        assert response.data['statusDetails']['ignoreUserWindow'] == snooze.user_window
        assert response.data['statusDetails']['ignoreUntil'] == snooze.until
        assert response.data['statusDetails']['actor']['id'] == six.text_type(self.user.id)

    def test_snooze_count(self):
        group = self.create_group(
            checksum='a' * 32,
            status=GroupStatus.RESOLVED,
            times_seen=1,
        )

        self.login_as(user=self.user)

        url = '{url}?id={group.id}'.format(
            url=self.path,
            group=group,
        )
        response = self.client.put(
            url, data={
                'status': 'ignored',
                'ignoreCount': 100,
            }, format='json'
        )

        assert response.status_code == 200

        snooze = GroupSnooze.objects.get(group=group)
        assert snooze.count == 100
        assert snooze.until is None
        assert snooze.user_count is None
        assert snooze.user_window is None
        assert snooze.window is None
        assert snooze.state['times_seen'] == 1

        assert response.data['status'] == 'ignored'
        assert response.data['statusDetails']['ignoreCount'] == snooze.count
        assert response.data['statusDetails']['ignoreWindow'] == snooze.window
        assert response.data['statusDetails']['ignoreUserCount'] == snooze.user_count
        assert response.data['statusDetails']['ignoreUserWindow'] == snooze.user_window
        assert response.data['statusDetails']['ignoreUntil'] == snooze.until
        assert response.data['statusDetails']['actor']['id'] == six.text_type(self.user.id)

    def test_snooze_user_count(self):
        group = self.create_group(
            checksum='a' * 32,
            status=GroupStatus.RESOLVED,
        )
        tagstore.create_group_tag_key(
            group.project_id,
            group.id,
            None,
            'sentry:user',
            values_seen=100,
        )

        self.login_as(user=self.user)

        url = '{url}?id={group.id}'.format(
            url=self.path,
            group=group,
        )
        response = self.client.put(
            url, data={
                'status': 'ignored',
                'ignoreUserCount': 100,
            }, format='json'
        )

        assert response.status_code == 200

        snooze = GroupSnooze.objects.get(group=group)
        assert snooze.count is None
        assert snooze.until is None
        assert snooze.user_count == 100
        assert snooze.user_window is None
        assert snooze.window is None
        assert snooze.state['users_seen'] == 100

        assert response.data['status'] == 'ignored'
        assert response.data['statusDetails']['ignoreCount'] == snooze.count
        assert response.data['statusDetails']['ignoreWindow'] == snooze.window
        assert response.data['statusDetails']['ignoreUserCount'] == snooze.user_count
        assert response.data['statusDetails']['ignoreUserWindow'] == snooze.user_window
        assert response.data['statusDetails']['ignoreUntil'] == snooze.until
        assert response.data['statusDetails']['actor']['id'] == six.text_type(self.user.id)

    def test_set_bookmarked(self):
        group1 = self.create_group(checksum='a' * 32, status=GroupStatus.RESOLVED)
        group2 = self.create_group(checksum='b' * 32, status=GroupStatus.UNRESOLVED)
        group3 = self.create_group(checksum='c' * 32, status=GroupStatus.IGNORED)
        group4 = self.create_group(
            project=self.create_project(slug='foo'),
            checksum='b' * 32,
            status=GroupStatus.UNRESOLVED
        )

        self.login_as(user=self.user)
        url = '{url}?id={group1.id}&id={group2.id}&group4={group4.id}'.format(
            url=self.path,
            group1=group1,
            group2=group2,
            group4=group4,
        )
        response = self.client.put(
            url, data={
                'isBookmarked': 'true',
            }, format='json'
        )
        assert response.status_code == 200
        assert response.data == {
            'isBookmarked': True,
        }

        bookmark1 = GroupBookmark.objects.filter(group=group1, user=self.user)
        assert bookmark1.exists()

        assert GroupSubscription.objects.filter(
            user=self.user,
            group=group1,
            is_active=True,
        ).exists()

        bookmark2 = GroupBookmark.objects.filter(group=group2, user=self.user)
        assert bookmark2.exists()

        assert GroupSubscription.objects.filter(
            user=self.user,
            group=group2,
            is_active=True,
        ).exists()

        bookmark3 = GroupBookmark.objects.filter(group=group3, user=self.user)
        assert not bookmark3.exists()

        bookmark4 = GroupBookmark.objects.filter(group=group4, user=self.user)
        assert not bookmark4.exists()

    def test_subscription(self):
        group1 = self.create_group(checksum='a' * 32)
        group2 = self.create_group(checksum='b' * 32)
        group3 = self.create_group(checksum='c' * 32)
        group4 = self.create_group(project=self.create_project(slug='foo'), checksum='b' * 32)

        self.login_as(user=self.user)
        url = '{url}?id={group1.id}&id={group2.id}&group4={group4.id}'.format(
            url=self.path,
            group1=group1,
            group2=group2,
            group4=group4,
        )
        response = self.client.put(
            url, data={
                'isSubscribed': 'true',
            }, format='json'
        )
        assert response.status_code == 200
        assert response.data == {
            'isSubscribed': True,
            'subscriptionDetails': {
                'reason': 'unknown',
            },
        }

        assert GroupSubscription.objects.filter(
            group=group1,
            user=self.user,
            is_active=True,
        ).exists()

        assert GroupSubscription.objects.filter(
            group=group2,
            user=self.user,
            is_active=True,
        ).exists()

        assert not GroupSubscription.objects.filter(
            group=group3,
            user=self.user,
        ).exists()

        assert not GroupSubscription.objects.filter(
            group=group4,
            user=self.user,
        ).exists()

    def test_set_public(self):
        group1 = self.create_group(checksum='a' * 32)
        group2 = self.create_group(checksum='b' * 32)

        self.login_as(user=self.user)
        url = '{url}?id={group1.id}&id={group2.id}'.format(
            url=self.path,
            group1=group1,
            group2=group2,
        )
        response = self.client.put(
            url, data={
                'isPublic': 'true',
            }, format='json'
        )
        assert response.status_code == 200
        assert response.data['isPublic'] is True
        assert 'shareId' in response.data

        new_group1 = Group.objects.get(id=group1.id)
        assert bool(new_group1.get_share_id())

        new_group2 = Group.objects.get(id=group2.id)
        assert bool(new_group2.get_share_id())

    def test_set_private(self):
        group1 = self.create_group(checksum='a' * 32)
        group2 = self.create_group(checksum='b' * 32)

        # Manually mark them as shared
        for g in group1, group2:
            GroupShare.objects.create(
                project_id=g.project_id,
                group=g,
            )
            assert bool(g.get_share_id())

        self.login_as(user=self.user)
        url = '{url}?id={group1.id}&id={group2.id}'.format(
            url=self.path,
            group1=group1,
            group2=group2,
        )
        response = self.client.put(
            url, data={
                'isPublic': 'false',
            }, format='json'
        )
        assert response.status_code == 200
        assert response.data == {
            'isPublic': False,
        }

        new_group1 = Group.objects.get(id=group1.id)
        assert not bool(new_group1.get_share_id())

        new_group2 = Group.objects.get(id=group2.id)
        assert not bool(new_group2.get_share_id())

    def test_set_has_seen(self):
        group1 = self.create_group(checksum='a' * 32, status=GroupStatus.RESOLVED)
        group2 = self.create_group(checksum='b' * 32, status=GroupStatus.UNRESOLVED)
        group3 = self.create_group(checksum='c' * 32, status=GroupStatus.IGNORED)
        group4 = self.create_group(
            project=self.create_project(slug='foo'),
            checksum='b' * 32,
            status=GroupStatus.UNRESOLVED
        )

        self.login_as(user=self.user)
        url = '{url}?id={group1.id}&id={group2.id}&group4={group4.id}'.format(
            url=self.path,
            group1=group1,
            group2=group2,
            group4=group4,
        )
        response = self.client.put(
            url, data={
                'hasSeen': 'true',
            }, format='json'
        )
        assert response.status_code == 200
        assert response.data == {
            'hasSeen': True,
        }

        r1 = GroupSeen.objects.filter(group=group1, user=self.user)
        assert r1.exists()

        r2 = GroupSeen.objects.filter(group=group2, user=self.user)
        assert r2.exists()

        r3 = GroupSeen.objects.filter(group=group3, user=self.user)
        assert not r3.exists()

        r4 = GroupSeen.objects.filter(group=group4, user=self.user)
        assert not r4.exists()

    @patch('sentry.api.endpoints.project_group_index.uuid4')
    @patch('sentry.api.endpoints.project_group_index.merge_group')
    def test_merge(self, merge_group, mock_uuid4):
        class uuid(object):
            hex = 'abc123'

        mock_uuid4.return_value = uuid
        group1 = self.create_group(checksum='a' * 32, times_seen=1)
        group2 = self.create_group(checksum='b' * 32, times_seen=50)
        group3 = self.create_group(checksum='c' * 32, times_seen=2)
        self.create_group(checksum='d' * 32)

        self.login_as(user=self.user)
        url = '{url}?id={group1.id}&id={group2.id}&id={group3.id}'.format(
            url=self.path,
            group1=group1,
            group2=group2,
            group3=group3,
        )
        response = self.client.put(
            url, data={
                'merge': '1',
            }, format='json'
        )
        assert response.status_code == 200
        assert response.data['merge']['parent'] == six.text_type(group2.id)
        assert sorted(response.data['merge']['children']) == sorted(
            [
                six.text_type(group1.id),
                six.text_type(group3.id),
            ]
        )

        assert len(merge_group.mock_calls) == 2
        merge_group.delay.assert_any_call(
            from_object_id=group1.id,
            to_object_id=group2.id,
            transaction_id='abc123',
        )
        merge_group.delay.assert_any_call(
            from_object_id=group3.id,
            to_object_id=group2.id,
            transaction_id='abc123',
        )

    def test_assign(self):
        group1 = self.create_group(checksum='a' * 32, is_public=True)
        group2 = self.create_group(checksum='b' * 32, is_public=True)
        user = self.user

        self.login_as(user=user)
        url = '{url}?id={group1.id}'.format(
            url=self.path,
            group1=group1,
        )
        response = self.client.put(
            url, data={
                'assignedTo': user.username,
            }
        )

        assert response.status_code == 200
        assert response.data['assignedTo']['id'] == six.text_type(user.id)
        assert response.data['assignedTo']['type'] == 'user'
        assert GroupAssignee.objects.filter(group=group1, user=user).exists()

        assert not GroupAssignee.objects.filter(group=group2, user=user).exists()

        assert Activity.objects.filter(
            group=group1,
            user=user,
            type=Activity.ASSIGNED,
        ).count() == 1

        assert GroupSubscription.objects.filter(
            user=user,
            group=group1,
            is_active=True,
        ).exists()

        response = self.client.put(
            url, data={
                'assignedTo': '',
            }, format='json'
        )

        assert response.status_code == 200, response.content
        assert response.data['assignedTo'] is None

        assert not GroupAssignee.objects.filter(group=group1, user=user).exists()

    def test_assign_non_member(self):
        group = self.create_group(checksum='a' * 32, is_public=True)
        member = self.user
        non_member = self.create_user('bar@example.com')

        self.login_as(user=member)

        url = '{url}?id={group.id}'.format(
            url=self.path,
            group=group,
        )
        response = self.client.put(
            url, data={
                'assignedTo': non_member.username,
            }, format='json'
        )

        assert response.status_code == 400, response.content

    def test_assign_team(self):
        self.login_as(user=self.user)

        group = self.create_group()
        other_member = self.create_user('bar@example.com')
        team = self.create_team(
            organization=group.project.organization, members=[
                self.user, other_member])

        group.project.add_team(team)

        url = '{url}?id={group.id}'.format(
            url=self.path,
            group=group,
        )
        response = self.client.put(
            url, data={
                'assignedTo': u'team:{}'.format(team.id),
            }
        )

        assert response.status_code == 200
        assert response.data['assignedTo']['id'] == six.text_type(team.id)
        assert response.data['assignedTo']['type'] == 'team'
        assert GroupAssignee.objects.filter(group=group, team=team).exists()

        assert Activity.objects.filter(
            group=group,
            type=Activity.ASSIGNED,
        ).count() == 1

        assert GroupSubscription.objects.filter(
            group=group,
            is_active=True,
        ).count() == 2

        response = self.client.put(
            url, data={
                'assignedTo': '',
            }, format='json'
        )

        assert response.status_code == 200, response.content
        assert response.data['assignedTo'] is None

    def test_discard(self):
        group1 = self.create_group(checksum='a' * 32, is_public=True)
        group2 = self.create_group(checksum='b' * 32, is_public=True)
        group_hash = GroupHash.objects.create(
            hash='x' * 32,
            project=group1.project,
            group=group1,
        )
        user = self.user

        self.login_as(user=user)
        url = '{url}?id={group1.id}'.format(
            url=self.path,
            group1=group1,
        )
        with self.tasks():
            with self.feature('projects:discard-groups'):
                response = self.client.put(
                    url, data={
                        'discard': True,
                    }
                )

        assert response.status_code == 204
        assert not Group.objects.filter(
            id=group1.id,
        ).exists()
        assert Group.objects.filter(
            id=group2.id,
        ).exists()
        assert GroupHash.objects.filter(
            id=group_hash.id,
        ).exists()
        tombstone = GroupTombstone.objects.get(
            id=GroupHash.objects.get(id=group_hash.id).group_tombstone_id,
        )
        assert tombstone.message == group1.message
        assert tombstone.culprit == group1.culprit
        assert tombstone.project == group1.project
        assert tombstone.data == group1.data


class GroupDeleteTest(APITestCase):
    @fixture
    def path(self):
        return '/api/0/projects/{}/{}/issues/'.format(
            self.project.organization.slug,
            self.project.slug,
        )

    def test_delete_by_id(self):
        group1 = self.create_group(checksum='a' * 32, status=GroupStatus.RESOLVED)
        group2 = self.create_group(checksum='b' * 32, status=GroupStatus.UNRESOLVED)
        group3 = self.create_group(checksum='c' * 32, status=GroupStatus.IGNORED)
        group4 = self.create_group(
            project=self.create_project(slug='foo'),
            checksum='b' * 32,
            status=GroupStatus.UNRESOLVED
        )

        for g in group1, group2, group3, group4:
            GroupHash.objects.create(
                project=g.project,
                hash=uuid4().hex,
                group=g,
            )

        self.login_as(user=self.user)
        url = '{url}?id={group1.id}&id={group2.id}&group4={group4.id}'.format(
            url=self.path,
            group1=group1,
            group2=group2,
            group4=group4,
        )

        response = self.client.delete(url, format='json')

        assert response.status_code == 204

        assert Group.objects.get(id=group1.id).status == GroupStatus.PENDING_DELETION
        assert not GroupHash.objects.filter(group_id=group1.id).exists()

        assert Group.objects.get(id=group2.id).status == GroupStatus.PENDING_DELETION
        assert not GroupHash.objects.filter(group_id=group2.id).exists()

        assert Group.objects.get(id=group3.id).status != GroupStatus.PENDING_DELETION
        assert GroupHash.objects.filter(group_id=group3.id).exists()

        assert Group.objects.get(id=group4.id).status != GroupStatus.PENDING_DELETION
        assert GroupHash.objects.filter(group_id=group4.id).exists()

        Group.objects.filter(id__in=(group1.id, group2.id)).update(status=GroupStatus.UNRESOLVED)

        with self.tasks():
            response = self.client.delete(url, format='json')

        assert response.status_code == 204

        assert not Group.objects.filter(id=group1.id).exists()
        assert not GroupHash.objects.filter(group_id=group1.id).exists()

        assert not Group.objects.filter(id=group2.id).exists()
        assert not GroupHash.objects.filter(group_id=group2.id).exists()

        assert Group.objects.filter(id=group3.id).exists()
        assert GroupHash.objects.filter(group_id=group3.id).exists()

        assert Group.objects.filter(id=group4.id).exists()
        assert GroupHash.objects.filter(group_id=group4.id).exists()

    def test_bulk_delete(self):
        groups = []
        for i in range(10, 41):
            groups.append(
                self.create_group(
                    project=self.project,
                    checksum=six.binary_type(i) * 16,
                    status=GroupStatus.RESOLVED))

        for group in groups:
            GroupHash.objects.create(
                project=group.project,
                hash=uuid4().hex,
                group=group,
            )

        self.login_as(user=self.user)

        # if query is '' it defaults to is:unresolved
        url = self.path + '?query='
        response = self.client.delete(url, format='json')

        assert response.status_code == 204

        for group in groups:
            assert Group.objects.get(id=group.id).status == GroupStatus.PENDING_DELETION
            assert not GroupHash.objects.filter(group_id=group.id).exists()

        Group.objects.filter(
            id__in=[
                group.id for group in groups]).update(
            status=GroupStatus.UNRESOLVED)

        with self.tasks():
            response = self.client.delete(url, format='json')

        assert response.status_code == 204

        for group in groups:
            assert not Group.objects.filter(id=group.id).exists()
            assert not GroupHash.objects.filter(group_id=group.id).exists()
