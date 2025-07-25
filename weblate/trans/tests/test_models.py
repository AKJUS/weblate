# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Test for translation models."""

import os
from datetime import timedelta
from unittest.mock import patch

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.management.color import no_style
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import override_settings
from django.utils import timezone

from weblate.auth.models import Group, User
from weblate.checks.models import Check
from weblate.formats.base import UnitNotFoundError
from weblate.lang.models import Language, Plural
from weblate.trans.actions import ActionEvents
from weblate.trans.exceptions import SuggestionSimilarToTranslationError
from weblate.trans.models import (
    Announcement,
    AutoComponentList,
    Change,
    Comment,
    Component,
    ComponentList,
    PendingUnitChange,
    Project,
    Suggestion,
    Translation,
    Unit,
    Vote,
)
from weblate.trans.models.project import CommitPolicyChoices
from weblate.trans.tests.utils import (
    RepoTestMixin,
    create_another_user,
    create_test_user,
)
from weblate.utils.django_hacks import immediate_on_commit, immediate_on_commit_leave
from weblate.utils.files import remove_tree
from weblate.utils.state import STATE_APPROVED, STATE_FUZZY, STATE_TRANSLATED


def fixup_languages_seq() -> None:
    # Reset sequence for Language and Plural objects as
    # we're manipulating with them in FixtureTestCase.setUpTestData
    # and that seems to affect sequence for other tests as well
    # on some PostgreSQL versions (probably sequence is not rolled back
    # in a transaction).
    commands = connection.ops.sequence_reset_sql(no_style(), [Language, Plural])
    if commands:
        with connection.cursor() as cursor:
            for sql in commands:
                cursor.execute(sql)
    # Invalidate object cache for languages
    Language.objects.flush_object_cache()


class BaseTestCase(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        fixup_languages_seq()

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        immediate_on_commit(cls)

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        immediate_on_commit_leave(cls)


class BaseLiveServerTestCase(StaticLiveServerTestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        fixup_languages_seq()

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        immediate_on_commit(cls)

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        immediate_on_commit_leave(cls)


class RepoTestCase(BaseTestCase, RepoTestMixin):
    """Generic class for tests working with repositories."""

    def setUp(self) -> None:
        self.clone_test_repos()


class ProjectTest(RepoTestCase):
    """Project object testing."""

    def test_create(self) -> None:
        project = self.create_project()
        self.assertTrue(os.path.exists(project.full_path))
        self.assertIn(project.slug, project.full_path)

    def test_rename(self) -> None:
        component = self.create_link()
        self.assertTrue(Component.objects.filter(repo="weblate://test/test").exists())
        project = component.project
        old_path = project.full_path
        self.assertTrue(os.path.exists(old_path))
        self.assertTrue(
            os.path.exists(
                component.translation_set.get(language_code="cs").get_filename()
            )
        )
        project.name = "Changed"
        project.slug = "changed"
        project.save()
        new_path = project.full_path
        self.addCleanup(remove_tree, new_path, True)
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(new_path))
        self.assertTrue(
            Component.objects.filter(repo="weblate://changed/test").exists()
        )
        self.assertFalse(Component.objects.filter(repo="weblate://test/test").exists())
        component = Component.objects.get(pk=component.pk)
        self.assertTrue(
            os.path.exists(
                component.translation_set.get(language_code="cs").get_filename()
            )
        )

    def test_delete(self) -> None:
        project = self.create_project()
        self.assertTrue(os.path.exists(project.full_path))
        project.delete()
        self.assertFalse(os.path.exists(project.full_path))

    def test_delete_votes(self) -> None:
        with transaction.atomic():
            component = self.create_po(
                suggestion_voting=True, suggestion_autoaccept=True
            )
            user = create_test_user()
            translation = component.translation_set.get(language_code="cs")
            unit = translation.unit_set.get(source="Hello, world!\n")
            suggestion = Suggestion.objects.add(unit, ["Test"], None)
            Vote.objects.create(suggestion=suggestion, value=Vote.POSITIVE, user=user)
        component.project.delete()

    def test_add_suggestion_validation(self) -> None:
        with transaction.atomic():
            component = self.create_po(
                suggestion_voting=True, suggestion_autoaccept=True
            )
            user = create_test_user()
            another_user = create_another_user()
            translation = component.translation_set.get(language_code="cs")
            unit = translation.unit_set.get(source="Hello, world!\n")

            unit.translate(user, "Translation of unit ", STATE_TRANSLATED)

            # check that another user submitting the same target raises an error
            with self.assertRaises(SuggestionSimilarToTranslationError):
                Suggestion.objects.add(
                    unit,
                    ["Translation of unit"],
                    None,
                    user=another_user,
                    raise_exception=True,
                )

            # check that same operation doesn't raise an error if raise_exception=False but returns false
            self.assertFalse(
                Suggestion.objects.add(
                    unit,
                    ["Translation of unit"],
                    None,
                    user=another_user,
                    raise_exception=False,
                )
            )

            # check that user submitting suggestion twice doesn't create duplicated suggestions
            suggestion = Suggestion.objects.add(
                unit, ["New suggestion"], None, user=another_user, raise_exception=True
            )
            suggestion_count = Suggestion.objects.count()
            self.assertTrue(bool(suggestion))
            suggestion = Suggestion.objects.add(
                unit, ["New suggestion"], None, user=another_user, raise_exception=True
            )
            self.assertFalse(suggestion)
            self.assertEqual(suggestion_count, Suggestion.objects.count())

    def test_delete_all(self) -> None:
        project = self.create_project()
        self.assertTrue(os.path.exists(project.full_path))
        Project.objects.all().delete()
        self.assertFalse(os.path.exists(project.full_path))

    def test_acl(self) -> None:
        """Test for ACL handling."""
        # Create user to verify ACL
        user = create_test_user()

        # Create project
        project = self.create_project()

        # Enable ACL
        project.access_control = Project.ACCESS_PRIVATE
        project.save()

        # Check user does not have access
        self.assertFalse(user.can_access_project(project))

        # Add to ACL group
        user.groups.add(Group.objects.get(name="Translate", defining_project=project))

        # Need to fetch user again to clear permission cache
        user = User.objects.get(username="testuser")

        # We now should have access
        self.assertTrue(user.can_access_project(project))


class TranslationTest(RepoTestCase):
    """Translation testing."""

    def test_basic(self) -> None:
        component = self.create_component()
        # Verify source translation
        translation = component.source_translation
        self.assertFalse(translation.unit_set.filter(num_words=0).exists())
        self.assertEqual(translation.stats.translated, 4)
        self.assertEqual(translation.stats.all, 4)
        self.assertEqual(translation.stats.fuzzy, 0)
        self.assertEqual(translation.stats.all_words, 19)
        # Verify target translation
        translation = component.translation_set.get(language_code="cs")
        self.assertEqual(translation.stats.translated, 0)
        self.assertEqual(translation.stats.all, 4)
        self.assertEqual(translation.stats.fuzzy, 0)
        self.assertEqual(translation.stats.all_words, 19)

    def test_validation(self) -> None:
        """Translation validation."""
        component = self.create_component()
        translation = component.translation_set.get(language_code="cs")
        translation.full_clean()

    def test_update_stats(self) -> None:
        """Check update stats with no units."""
        component = self.create_component()
        translation = component.translation_set.get(language_code="cs")
        self.assertEqual(translation.stats.all, 4)
        self.assertEqual(translation.stats.all_words, 19)
        translation.unit_set.all().delete()
        translation.invalidate_cache()
        self.assertEqual(translation.stats.all, 0)
        self.assertEqual(translation.stats.all_words, 0)

    def test_commit_grouping(self) -> None:
        component = self.create_component()
        translation = component.translation_set.get(language_code="cs")
        user = create_test_user()
        start_rev = component.repository.last_revision
        units = list(translation.unit_set.all())
        # translations by same author in one commit
        units[0].translate(user, "test1", STATE_TRANSLATED)
        units[1].translate(user, "test2", STATE_TRANSLATED)
        count = 1
        # translations by different author in different commits
        for unit in [units[2], units[3]]:
            user = User.objects.create(
                full_name=f"User {unit.pk}",
                username=f"user-{unit.pk}",
                email=f"{unit.pk}@example.com",
            )
            unit.translate(user, "test", STATE_TRANSLATED)
            count += 1
        # no instant automatic commit
        self.assertEqual(start_rev, component.repository.last_revision)
        self.assertEqual(translation.count_pending_units, 4)

        with patch.object(
            translation.store, "find_unit", side_effect=UnitNotFoundError("test error")
        ):
            # Check failure in committing pending changes retains them in db
            # use _commit_pending, patching won't work if the Translation object
            # is loaded from the db
            translation._commit_pending("test", None)  # noqa: SLF001
            self.assertEqual(translation.count_pending_units, 4)

        # Commit pending changes
        translation.commit_pending("test", None)
        self.assertNotEqual(start_rev, component.repository.last_revision)
        self.assertEqual(component.repository.count_outgoing(), count)
        self.assertEqual(translation.count_pending_units, 0)

    def test_group_changes_by_author(self) -> None:
        component = self.create_component()
        translation = component.translation_set.get(language_code="cs")
        user1 = create_test_user()
        user2 = create_another_user()

        units = list(translation.unit_set.all())
        units[0].translate(user1, "test1", STATE_TRANSLATED)
        units[1].translate(user2, "test2", STATE_TRANSLATED)
        units[2].translate(user1, "test3", STATE_TRANSLATED)
        # change conflicts with user 2's edit on unit 1,
        # user 2's changes will be split into a separate commit
        # to ensure it is applied first
        units[1].translate(user1, "test2!", STATE_TRANSLATED)
        units[3].translate(user2, "test4", STATE_TRANSLATED)

        all_changes = list(
            PendingUnitChange.objects.for_translation(translation).order_by("timestamp")
        )
        groups = Translation._group_changes_by_author(all_changes)  # noqa: SLF001
        self.assertEqual(len(groups), 3)

        author, changes = groups[0]
        self.assertEqual(author, user2)
        self.assertEqual(len(changes), 1)
        self.assertEqual([c.unit for c in changes], [units[1]])

        author, changes = groups[1]
        self.assertEqual(author, user1)
        self.assertEqual(len(changes), 3)
        self.assertEqual([c.unit for c in changes], [units[0], units[2], units[1]])

        author, changes = groups[2]
        self.assertEqual(author, user2)
        self.assertEqual(len(changes), 1)
        self.assertEqual([c.unit for c in changes], [units[3]])

    def test_group_changes_by_author_single_author(self) -> None:
        """Test that multiple changes to a unit by single author are grouped."""
        component = self.create_component()
        translation = component.translation_set.get(language_code="cs")
        user = create_test_user()

        units = list(translation.unit_set.all())
        units[0].translate(user, "test1", STATE_TRANSLATED)
        units[1].translate(user, "test2", STATE_TRANSLATED)
        units[2].translate(user, "test3", STATE_TRANSLATED)
        # change conflicts with earlier change to unit but
        # don't split groups as all changes from same author
        units[1].translate(user, "test2!", STATE_TRANSLATED)
        units[3].translate(user, "test4", STATE_TRANSLATED)

        all_changes = list(
            PendingUnitChange.objects.for_translation(translation).order_by("timestamp")
        )
        groups = Translation._group_changes_by_author(all_changes)  # noqa: SLF001
        self.assertEqual(len(groups), 1)

        author, changes = groups[0]
        self.assertEqual(author, user)
        self.assertEqual(
            [c.unit for c in changes],
            [units[0], units[1], units[2], units[1], units[3]],
        )

    def test_commit_explanation(self):
        user = create_test_user()
        component = self.create_tbx()

        self.assertEqual(PendingUnitChange.objects.count(), 0)

        en_translation = component.translation_set.get(language_code="en")
        source_unit = en_translation.unit_set.get(source="address bar")
        source_unit.update_explanation("explanation 1", user)

        # one change for each translation file, source language does not have
        # a translation file
        self.assertFalse(PendingUnitChange.objects.filter(unit=source_unit).exists())
        self.assertEqual(
            PendingUnitChange.objects.count(), component.translation_set.count() - 1
        )
        component.commit_pending("test", None)

        self.assertEqual(PendingUnitChange.objects.count(), 0)

        cs_translation = component.translation_set.get(language_code="cs")
        target_unit = cs_translation.unit_set.get(source="address bar")
        target_unit.update_explanation("explanation 2", user)
        # only adds pending change for target unit's translation file
        self.assertEqual(PendingUnitChange.objects.count(), 1)

    def test_commit_policy(self):
        component = self.create_component()
        translation = component.translation_set.get(language_code="cs")
        user = create_test_user()

        project = component.project
        project.commit_policy = CommitPolicyChoices.APPROVED_ONLY
        project.save()

        unit1 = translation.unit_set.get(source="Hello, world!\n")
        unit1.translate(user, "Unit 1 - Test 1", STATE_TRANSLATED)
        unit1.translate(user, "Unit 1 - Test 2", STATE_FUZZY)
        unit1.translate(user, "Unit 1 - Test 3", STATE_APPROVED)
        unit1.translate(user, "Unit 1 - Test 4", STATE_FUZZY)

        unit2 = translation.unit_set.get(source="Thank you for using Weblate.")
        unit2.translate(user, "Unit 2 - Test 1", STATE_FUZZY)

        unit3 = translation.unit_set.get(
            source="Try Weblate at <https://demo.weblate.org/>!\n"
        )
        unit3.translate(user, "Unit 3 - Test 1", STATE_TRANSLATED)

        self.assertEqual(PendingUnitChange.objects.count(), 6)
        changes = list(PendingUnitChange.objects.for_translation(translation))
        self.assertEqual(len(changes), 3)
        self.assertTrue(all(p.unit == unit1 for p in changes))
        self.assertEqual(
            [STATE_TRANSLATED, STATE_FUZZY, STATE_APPROVED], [p.state for p in changes]
        )

        component.commit_pending("test", None)
        self.assertEqual(PendingUnitChange.objects.count(), 3)

        project.commit_policy = CommitPolicyChoices.WITHOUT_NEEDS_EDITING
        project.save()

        changes = list(PendingUnitChange.objects.for_translation(translation))
        self.assertEqual(len(changes), 1)
        self.assertTrue(all(p.unit == unit3 for p in changes))
        self.assertEqual([STATE_TRANSLATED], [p.state for p in changes])

        component.commit_pending("test", None)
        self.assertEqual(PendingUnitChange.objects.count(), 2)

        project.commit_policy = CommitPolicyChoices.ALL
        project.save()

        changes = list(PendingUnitChange.objects.for_translation(translation))
        self.assertEqual(len(changes), 2)
        self.assertEqual([STATE_FUZZY, STATE_FUZZY], [p.state for p in changes])
        component.commit_pending("test", None)
        self.assertEqual(PendingUnitChange.objects.count(), 0)


class ComponentListTest(RepoTestCase):
    """Test(s) for ComponentList model."""

    CREATE_GLOSSARIES: bool = True

    def test_slug(self) -> None:
        """Test ComponentList slug."""
        clist = ComponentList()
        clist.slug = "slug"
        self.assertEqual(clist.tab_slug(), "list-slug")

    def test_auto(self) -> None:
        self.create_component()
        clist = ComponentList.objects.create(name="Name", slug="slug")
        AutoComponentList.objects.create(
            project_match="^.*$", component_match="^.*$", componentlist=clist
        )
        self.assertEqual(clist.components.count(), 2)

    def test_auto_create(self) -> None:
        clist = ComponentList.objects.create(name="Name", slug="slug")
        AutoComponentList.objects.create(
            project_match="^.*$", component_match="^.*$", componentlist=clist
        )
        self.assertEqual(clist.components.count(), 0)
        self.create_component()
        self.assertEqual(clist.components.count(), 2)

    def test_auto_nomatch(self) -> None:
        self.create_component()
        clist = ComponentList.objects.create(name="Name", slug="slug")
        AutoComponentList.objects.create(
            project_match="^none$", component_match="^.*$", componentlist=clist
        )
        self.assertEqual(clist.components.count(), 0)


class ModelTestCase(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.component = self.create_component()


class SourceUnitTest(ModelTestCase):
    """Source Unit objects testing."""

    def test_source_unit(self) -> None:
        unit = Unit.objects.filter(translation__language_code="cs")[0]
        self.assertIsNotNone(unit.source_unit)
        unit = Unit.objects.filter(translation__language_code="en")[0]
        self.assertEqual(unit.source_unit, unit)

    def test_priority(self) -> None:
        unit = Unit.objects.filter(translation__language_code="cs")[0]
        self.assertEqual(unit.priority, 100)
        source = unit.source_unit
        source.extra_flags = "priority:200"
        source.save()
        unit2 = Unit.objects.get(pk=unit.pk)
        self.assertEqual(unit2.priority, 200)

    def test_check_flags(self) -> None:
        """Setting of Source check_flags changes checks for related units."""
        self.assertEqual(Check.objects.count(), 3)
        check = Check.objects.all()[0]
        unit = check.unit
        self.assertEqual(self.component.stats.allchecks, 3)
        source = unit.source_unit
        source.extra_flags = f"ignore-{check.name}"
        source.save()
        self.assertEqual(Check.objects.count(), 0)
        self.assertEqual(Component.objects.get(pk=self.component.pk).stats.allchecks, 0)


class UnitTest(ModelTestCase):
    def test_newlines(self) -> None:
        user = create_test_user()
        unit = Unit.objects.filter(
            translation__language_code="cs", source="Hello, world!\n"
        )[0]
        unit.translate(user, "new\nstring\n", STATE_TRANSLATED)
        self.assertEqual(unit.target, "new\nstring\n")
        # New object to clear all_flags cache
        unit = Unit.objects.get(pk=unit.pk)
        unit.flags = "dos-eol"
        unit.translate(user, "new\nstring", STATE_TRANSLATED)
        self.assertEqual(unit.target, "new\r\nstring\r\n")
        unit.translate(user, "other\r\nstring", STATE_TRANSLATED)
        self.assertEqual(unit.target, "other\r\nstring\r\n")

    def test_flags(self) -> None:
        unit = Unit.objects.filter(translation__language_code="cs")[0]
        unit.flags = "no-wrap, ignore-same"
        self.assertEqual(unit.all_flags.items(), {"no-wrap", "ignore-same"})

    def test_order_by_request(self) -> None:
        unit = Unit.objects.filter(translation__language_code="cs")[0]
        source = unit.source_unit
        source.extra_flags = "priority:200"
        source.save()

        # test both ascending and descending order works
        unit1 = Unit.objects.filter(translation__language_code="cs")
        unit1 = unit1.order_by_request({"sort_by": "-priority"}, None)
        self.assertEqual(unit1[0].priority, 200)
        unit1 = Unit.objects.filter(translation__language_code="cs")
        unit1 = unit1.order_by_request({"sort_by": "priority"}, None)
        self.assertEqual(unit1[0].priority, 100)

        # test if invalid sorting, then sorted in default order
        unit2 = Unit.objects.filter(translation__language_code="cs")
        unit2 = unit2.order()
        unit3 = Unit.objects.filter(translation__language_code="cs")
        unit3 = unit3.order_by_request({"sort_by": "invalid"}, None)
        self.assertEqual(unit3[0], unit2[0])

        # test sorting by count
        unit4 = Unit.objects.filter(translation__language_code="cs")[2]
        Comment.objects.create(unit=unit4, comment="Foo")
        unit5 = Unit.objects.filter(translation__language_code="cs")
        unit5 = unit5.order_by_request({"sort_by": "-num_comments"}, None)
        self.assertEqual(unit5[0].comment_set.count(), 1)
        unit5 = Unit.objects.filter(translation__language_code="cs")
        unit5 = unit5.order_by_request({"sort_by": "num_comments"}, None)
        self.assertEqual(unit5[0].comment_set.count(), 0)

        # check all order options produce valid queryset
        order_options = [
            "priority",
            "position",
            "context",
            "num_words",
            "labels",
            "timestamp",
            "num_failing_checks",
        ]
        for order_option in order_options:
            ordered_unit = Unit.objects.filter(
                translation__language_code="cs"
            ).order_by_request({"sort_by": order_option}, None)
            ordered_desc_unit = Unit.objects.filter(
                translation__language_code="cs"
            ).order_by_request({"sort_by": f"-{order_option}"}, None)
            self.assertEqual(len(ordered_unit), 4)
            self.assertEqual(len(ordered_desc_unit), 4)

        # check sorting with multiple options work
        multiple_ordered_unit = Unit.objects.filter(
            translation__language_code="cs"
        ).order_by_request({"sort_by": "position,timestamp"}, None)
        self.assertEqual(multiple_ordered_unit.count(), 4)

    def test_get_max_length_no_pk(self) -> None:
        unit = Unit.objects.filter(translation__language_code="cs")[0]
        unit.pk = False
        self.assertEqual(unit.get_max_length(), 10000)

    def test_get_max_length_empty_source_default_fallback(self) -> None:
        unit = Unit.objects.filter(translation__language_code="cs")[0]
        unit.pk = True
        unit.source = ""
        self.assertEqual(unit.get_max_length(), 100)

    def test_get_max_length_default_fallback(self) -> None:
        unit = Unit.objects.filter(translation__language_code="cs")[0]
        unit.pk = True
        unit.source = "My test source"
        self.assertEqual(unit.get_max_length(), 140)

    @override_settings(LIMIT_TRANSLATION_LENGTH_BY_SOURCE_LENGTH=False)
    def test_get_max_length_empty_source_disabled_default_fallback(self) -> None:
        unit = Unit.objects.filter(translation__language_code="cs")[0]
        unit.pk = True
        unit.source = ""
        self.assertEqual(unit.get_max_length(), 10000)

    @override_settings(LIMIT_TRANSLATION_LENGTH_BY_SOURCE_LENGTH=False)
    def test_get_max_length_disabled_default_fallback(self) -> None:
        unit = Unit.objects.filter(translation__language_code="cs")[0]
        unit.pk = True
        unit.source = "My test source"
        self.assertEqual(unit.get_max_length(), 10000)


class AnnouncementTest(ModelTestCase):
    """Test(s) for Announcement model."""

    def setUp(self) -> None:
        super().setUp()
        Announcement.objects.create(
            language=Language.objects.get(code="cs"), message="test cs"
        )
        Announcement.objects.create(
            language=Language.objects.get(code="de"), message="test de"
        )
        Announcement.objects.create(
            project=self.component.project, message="test project"
        )
        Announcement.objects.create(
            component=self.component,
            project=self.component.project,
            message="test component",
        )
        Announcement.objects.create(message="test global")

    def verify_filter(self, messages, count, message=None) -> None:
        """Verify whether messages have given count and first contains string."""
        self.assertEqual(len(messages), count)
        if message is not None:
            self.assertEqual(messages[0].message, message)

    def test_contextfilter_global(self) -> None:
        self.verify_filter(Announcement.objects.context_filter(), 1, "test global")

    def test_contextfilter_project(self) -> None:
        self.verify_filter(
            Announcement.objects.context_filter(project=self.component.project),
            1,
            "test project",
        )

    def test_contextfilter_component(self) -> None:
        self.verify_filter(
            Announcement.objects.context_filter(component=self.component), 2
        )

    def test_contextfilter_translation(self) -> None:
        self.verify_filter(
            Announcement.objects.context_filter(
                component=self.component, language=Language.objects.get(code="cs")
            ),
            3,
        )

    def test_contextfilter_language(self) -> None:
        self.verify_filter(
            Announcement.objects.context_filter(
                language=Language.objects.get(code="cs")
            ),
            1,
            "test cs",
        )
        self.verify_filter(
            Announcement.objects.context_filter(
                language=Language.objects.get(code="de")
            ),
            1,
            "test de",
        )


class ChangeTest(ModelTestCase):
    """Test(s) for Change model."""

    def test_day_filtering(self) -> None:
        Change.objects.all().delete()
        for days_since in range(3):
            change = Change.objects.create(action=ActionEvents.CREATE_PROJECT)
            change.timestamp -= timedelta(days=days_since)
            change.save()

        # filter by day with date
        self.assertEqual(
            Change.objects.filter_by_day(
                timezone.now().date() - timedelta(days=1)
            ).count(),
            1,
        )
        # filter by day with datetime
        self.assertEqual(
            Change.objects.filter_by_day(timezone.now() - timedelta(days=1)).count(),
            1,
        )

        # filter since_day with date
        self.assertEqual(
            Change.objects.since_day(timezone.now().date() - timedelta(days=1)).count(),
            2,
        )
        # filter since_day with datetime
        self.assertEqual(
            Change.objects.since_day(timezone.now() - timedelta(days=1)).count(),
            2,
        )
