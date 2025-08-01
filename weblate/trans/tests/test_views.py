# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Test for translation views."""

from __future__ import annotations

from io import BytesIO
from unittest import TestCase
from urllib.parse import urlsplit
from zipfile import ZipFile

from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test.client import RequestFactory
from django.test.utils import override_settings
from django.urls import reverse
from django.utils.translation import activate
from openpyxl import load_workbook
from PIL import Image

from weblate.auth.models import Group, User, get_anonymous, setup_project_groups
from weblate.lang.models import Language
from weblate.trans.models import Component, ComponentList, Project, Translation, Unit
from weblate.trans.tests.test_models import RepoTestCase
from weblate.trans.tests.utils import (
    create_another_user,
    create_test_user,
    wait_for_celery,
)
from weblate.utils.hash import hash_to_checksum
from weblate.utils.xml import parse_xml


class RegistrationTestMixin(TestCase):
    """Helper to share code for registration testing."""

    def assert_registration_mailbox(self, match: str | None = None) -> str:
        if match is None:
            match = "[Weblate] Your registration on Weblate"
        # Check mailbox
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, match)

        live_url = getattr(self, "live_server_url", None)

        # Parse URL
        for line in mail.outbox[0].body.splitlines():
            if "verification_code" not in line:
                continue
            if "(" in line or ")" in line or "<" in line or ">" in line:
                continue
            if live_url and line.startswith(live_url):
                return line + "&confirm=1"
            if line.startswith("http://example.com/"):
                return line[18:] + "&confirm=1"

        self.fail("Confirmation URL not found")
        return ""

    def assert_notify_mailbox(self, sent_mail) -> None:
        self.assertEqual(
            sent_mail.subject, "[Weblate] Activity on your account at Weblate"
        )


class ViewTestCase(RepoTestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        get_anonymous.cache_clear()
        super().setUpTestData()

    def setUp(self) -> None:
        super().setUp()
        # Many tests needs access to the request factory.
        self.factory = RequestFactory()
        # Create user
        self.user = create_test_user()
        group = Group.objects.get(name="Users")
        self.user.groups.add(group)
        # Create another user
        self.anotheruser = create_another_user()
        self.user.groups.add(group)
        # Create project to have some test base
        self.component = self.create_component()
        self.project = self.component.project
        self.translation = self.get_translation()
        # Invalidate caches
        cache.clear()
        # Login
        self.client.login(username="testuser", password="testpassword")
        # Prepopulate kwargs

    @property
    def kw_project(self):
        return {"project": self.project.slug}

    @property
    def kw_project_path(self):
        return {"path": self.project.get_url_path()}

    @property
    def kw_component(self):
        return {"path": self.component.get_url_path()}

    @property
    def kw_translation(self):
        return {"path": self.translation.get_url_path()}

    @property
    def translation_url(self):
        return self.translation.get_absolute_url()

    @property
    def project_url(self):
        return self.project.get_absolute_url()

    @property
    def component_url(self):
        return self.component.get_absolute_url()

    def tearDown(self) -> None:
        super().tearDown()
        # Reset to English language
        activate("en")

    def update_fulltext_index(self) -> None:
        wait_for_celery()

    def make_manager(self) -> None:
        """Make user a Manager."""
        # Sitewide privileges
        self.user.groups.add(Group.objects.get(name="Managers"))
        # Project privileges
        self.project.add_user(self.user, "Administration")

    def get_request(self, user=None):
        """Get fake request object."""
        request = self.factory.get("/")
        request.user = user or self.user
        request.session = "session"
        messages = FallbackStorage(request)
        request._messages = messages  # noqa: SLF001
        return request

    def get_translation(self, language: str = "cs") -> Translation:
        return self.component.translation_set.get(language__code=language)

    def get_unit(
        self,
        source: str = "Hello, world!\n",
        language: str = "cs",
        translation: Translation | None = None,
    ) -> Unit:
        if translation is None:
            translation = self.get_translation(language)
        return translation.unit_set.get(source__startswith=source)

    def change_unit(
        self,
        target: str,
        source: str = "Hello, world!\n",
        language: str = "cs",
        user: User | None = None,
    ) -> None:
        unit = self.get_unit(source, language)
        unit.target = target
        unit.save_backend(user or self.user)

    def edit_unit(
        self,
        source: str,
        target: str,
        language: str = "cs",
        follow: bool = False,
        translation: Translation | None = None,
        **kwargs,
    ):
        """Do edit single unit using web interface."""
        unit = self.get_unit(source, language, translation=translation)
        params = {
            "checksum": unit.checksum,
            "contentsum": hash_to_checksum(unit.content_hash),
            "translationsum": hash_to_checksum(unit.get_target_hash()),
            "target_0": target,
            "review": "20",
        }
        params.update(kwargs)
        return self.client.post(
            unit.translation.get_translate_url(), params, follow=follow
        )

    def assert_redirects_offset(self, response, exp_path, exp_offset) -> None:
        """Assert that offset in response matches expected one."""
        self.assertEqual(response.status_code, 302)

        # We don't use all variables
        _scheme, _netloc, path, query, _fragment = urlsplit(response["Location"])

        self.assertEqual(path, exp_path)

        exp_offset = f"offset={exp_offset:d}"
        self.assertIn(exp_offset, query)

    def assert_png(self, response) -> None:
        """Check whether response contains valid PNG image."""
        # Check response status code
        self.assertEqual(response.status_code, 200)
        self.assert_png_data(response.content)

    def assert_png_data(self, content) -> None:
        """Check whether data is PNG image."""
        # Try to load PNG with PIL
        image = Image.open(BytesIO(content))
        self.assertEqual(image.format, "PNG")

    def assert_zip(self, response, filename: str | None = None):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        with ZipFile(BytesIO(response.content), "r") as zipfile:
            self.assertIsNone(zipfile.testzip())
            if filename is not None:
                self.assertIn(filename, zipfile.namelist())
                with zipfile.open(filename) as handle:
                    return handle.read()
            return None

    def assert_excel(self, response) -> None:
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            "; charset=utf-8",
        )
        load_workbook(BytesIO(response.content))

    def assert_svg(self, response) -> None:
        """Check whether response is a SVG image."""
        # Check response status code
        self.assertEqual(response.status_code, 200)
        tree = parse_xml(response.content)
        self.assertEqual(tree.tag, "{http://www.w3.org/2000/svg}svg")

    def assert_backend(
        self,
        expected_translated: int,
        language: str = "cs",
        translation: Translation | None = None,
    ) -> None:
        """Check that backend has correct data."""
        if translation is None:
            translation = self.get_translation(language)
        translation.commit_pending("test", None)
        store = translation.component.file_format_cls(translation.get_filename(), None)
        messages = set()
        translated = 0

        for unit in store.content_units:
            id_hash = unit.id_hash
            self.assertNotIn(id_hash, messages, "Duplicate string in in backend file!")
            if unit.is_translated():
                translated += 1

        self.assertEqual(
            translated,
            expected_translated,
            f"Did not found expected number of translations ({translated} != {expected_translated}).",
        )

    def log_as_jane(self) -> None:
        self.client.login(username="jane", password="testpassword")


class FixtureTestCase(ViewTestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        """Manually load fixture."""
        # Ensure there are no Language objects, we add
        # them in defined order in fixture
        Language.objects.all().delete()

        # Stolen from setUpClass, we just need to do it
        # after transaction checkpoint and deleting languages
        for db_name in cls._databases_names(include_mirrors=False):
            call_command(
                "loaddata", "simple-project.json", verbosity=0, database=db_name
            )
        # Apply group project/language automation
        for group in Group.objects.iterator():
            group.save()

        super().setUpTestData()

    def clone_test_repos(self) -> None:
        return

    def create_project(self):
        project = Project.objects.all()[0]
        setup_project_groups(self, project)
        return project

    def create_component(self):
        component = self.create_project().component_set.all()[0]
        component.create_path()
        return component


class TranslationManipulationTest(ViewTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.component.new_lang = "add"
        self.component.save()

    def create_component(self):
        return self.create_po_new_base()

    def test_model_add(self) -> None:
        self.assertTrue(
            self.component.add_new_language(
                Language.objects.get(code="af"), self.get_request()
            )
        )
        self.assertTrue(
            self.component.translation_set.filter(language_code="af").exists()
        )

    def test_model_add_duplicate(self) -> None:
        request = self.get_request()
        self.assertFalse(get_messages(request))
        self.assertIsNone(
            self.component.add_new_language(Language.objects.get(code="de"), request)
        )
        self.assertTrue(get_messages(request))

    def test_model_add_disabled(self) -> None:
        self.component.new_lang = "contact"
        self.component.save()
        self.assertFalse(
            self.component.add_new_language(
                Language.objects.get(code="af"), self.get_request()
            )
        )

    def test_model_add_superuser(self) -> None:
        self.component.new_lang = "contact"
        self.component.save()
        self.user.is_superuser = True
        self.user.save()
        self.assertTrue(
            self.component.add_new_language(
                Language.objects.get(code="af"), self.get_request()
            )
        )

    def test_remove(self) -> None:
        translation = self.component.translation_set.get(language_code="de")
        translation.remove(self.user)
        # Force scanning of the repository
        self.component.create_translations_immediate()
        self.assertFalse(
            self.component.translation_set.filter(language_code="de").exists()
        )


class ProjectLanguageAdditionTest(ViewTestCase):
    def setUp(self) -> None:
        super().setUp()
        # default test component is new_lang = "contact"
        self.components = {"contact": self.component}
        for new_lang in ["add", "none", "url"]:
            self.components[new_lang] = self.create_po_new_base(
                new_lang=new_lang,
                name=f"test-{new_lang}",
                project=self.project,
            )
        self.url = reverse("new-language", kwargs={"path": self.project.get_url_path()})

    def create_component(self):
        return self.create_po_new_base()

    def test_none_eligible(self):
        self.project.component_set.update(new_lang="none")

        self.user.is_superuser = True
        self.user.save()

        response = self.client.get(
            reverse("new-language", kwargs={"path": self.project.get_url_path()}),
            follow=True,
        )
        self.assertRedirects(response, self.project.get_absolute_url())

    def test_permission(self):
        self.project.access_control = Project.ACCESS_PROTECTED
        self.project.save(update_fields=["access_control"])
        response = self.client.get(
            reverse("new-language", kwargs={"path": self.project.get_url_path()}),
            follow=True,
        )
        self.assertEqual(response.status_code, 403)

    def test_existing_language_excluded(self):
        self.user.is_superuser = True
        self.user.save()

        response = self.client.post(
            reverse(
                "new-language", kwargs={"path": self.components["add"].get_url_path()}
            ),
            {"lang": "af"},
            follow=True,
        )
        self.assertTrue(
            self.components["add"].translation_set.filter(language_code="af").exists()
        )
        response = self.client.post(
            reverse(
                "new-language",
                kwargs={"path": self.components["contact"].get_url_path()},
            ),
            {"lang": "af"},
            follow=True,
        )
        self.assertTrue(
            self.components["contact"]
            .translation_set.filter(language_code="af")
            .exists()
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("form", response.context)
        codes = {c[0] for c in response.context["form"]["lang"].field.choices}
        self.assertNotIn("af", codes)

        response = self.client.post(self.url, {"lang": "af"}, follow=True)
        self.assertEqual(response.status_code, 200)
        messages = [m.message for m in response.context["messages"]]
        self.assertCountEqual(
            messages,
            [
                "Please fix errors in the form.",
            ],
        )

    def test_view_add_language(self) -> None:
        self.assertTrue(
            all(
                not c.translation_set.filter(language_code="af").exists()
                for c in self.components.values()
            )
        )
        response = self.client.post(self.url, {"lang": "af"}, follow=True)
        self.assertRedirects(response, self.project.get_absolute_url())

        messages = [m.message for m in response.context["messages"]]
        self.assertCountEqual(
            messages,
            [
                "Language Afrikaans added to 1 component.",
                "Language Afrikaans requested for 1 component.",
            ],
        )
        for new_lang, component in self.components.items():
            lang_exists = component.translation_set.filter(language_code="af").exists()
            self.assertEqual(lang_exists, new_lang == "add")

    def test_view_add_duplicate_language(self) -> None:
        """Test adding a language that already exists in some components."""
        # Add the language to one component directly
        self.assertTrue(
            all(
                not c.translation_set.filter(language_code="af").exists()
                for c in self.components.values()
            )
        )

        self.user.is_superuser = True
        self.user.save()
        self.assertTrue(
            self.components["contact"].add_new_language(
                Language.objects.get(code="af"), self.get_request()
            )
        )
        self.assertTrue(
            self.components["add"].add_new_language(
                Language.objects.get(code="pa"), self.get_request()
            )
        )
        self.user.is_superuser = False
        self.user.save()

        response = self.client.post(self.url, {"lang": "af"}, follow=True)
        self.assertRedirects(response, self.project.get_absolute_url())

        messages = [m.message for m in response.context["messages"]]
        self.assertCountEqual(
            messages,
            [
                "Language Afrikaans added to 1 component.",
            ],
        )

        response = self.client.post(self.url, {"lang": "pa"}, follow=True)
        self.assertRedirects(response, self.project.get_absolute_url())
        messages = [m.message for m in response.context["messages"]]
        self.assertCountEqual(
            messages,
            [
                "Language Punjabi requested for 1 component.",
                "Language Punjabi could not be added to 1 component. Please check the component's configuration.",
            ],
        )

    def test_view_add_language_superuser(self) -> None:
        self.user.is_superuser = True
        self.user.save()

        self.assertTrue(
            all(
                not c.translation_set.filter(language_code="af").exists()
                for c in self.components.values()
            )
        )
        response = self.client.post(self.url, {"lang": "af"}, follow=True)
        self.assertRedirects(response, self.project.get_absolute_url())

        messages = [m.message for m in response.context["messages"]]
        self.assertCountEqual(
            messages,
            [
                "Language Afrikaans added to 2 components.",
            ],
        )

        for new_lang, component in self.components.items():
            lang_exists = component.translation_set.filter(language_code="af").exists()
            self.assertEqual(lang_exists, new_lang in {"add", "contact"})


class BasicViewTest(ViewTestCase):
    def test_view_project(self) -> None:
        response = self.client.get(self.project.get_absolute_url())
        self.assertContains(response, "test/test")
        self.assertNotContains(response, "Spanish")

    def test_view_project_ghost(self) -> None:
        self.user.profile.languages.add(Language.objects.get(code="es"))
        response = self.client.get(self.project.get_absolute_url())
        self.assertContains(response, "Spanish")

    def test_view_component(self) -> None:
        response = self.client.get(self.component.get_absolute_url())
        self.assertContains(response, "Test/Test")
        self.assertNotContains(response, "Spanish")

    def test_view_component_ghost(self) -> None:
        self.user.profile.languages.add(Language.objects.get(code="es"))
        response = self.client.get(self.component.get_absolute_url())
        self.assertContains(response, "Spanish")

    def test_view_component_guide(self) -> None:
        response = self.client.get(reverse("guide", kwargs=self.kw_component))
        self.assertContains(response, "Test/Test")

    def test_view_translation(self) -> None:
        response = self.client.get(self.translation.get_absolute_url())
        self.assertContains(response, "Test/Test")

    def test_view_translation_others(self) -> None:
        with override_settings(CREATE_GLOSSARIES=self.CREATE_GLOSSARIES):
            other = Component.objects.create(
                name="RESX component",
                slug="resx",
                project=self.project,
                repo="weblate://test/test",
                file_format="resx",
                filemask="resx/*.resx",
                template="resx/en.resx",
                new_lang="add",
            )
        # Existing translation
        response = self.client.get(self.translation.get_absolute_url())
        self.assertContains(response, other.name)
        # Ghost translation
        kwargs = {"path": [*self.component.get_url_path(), "it"]}
        response = self.client.get(reverse("show", kwargs=kwargs))
        self.assertContains(response, other.name)

    def test_view_unit(self) -> None:
        unit = self.get_unit()
        response = self.client.get(unit.get_absolute_url())
        self.assertContains(response, "Hello, world!")

    def test_view_component_list(self) -> None:
        clist = ComponentList.objects.create(name="TestCL", slug="testcl")
        clist.components.add(self.component)
        response = self.client.get(reverse("component-list", kwargs={"name": "testcl"}))
        self.assertContains(response, "TestCL")
        self.assertContains(response, self.component.name)


class BasicMonolingualViewTest(BasicViewTest):
    def create_component(self):
        return self.create_po_mono()


class SourceStringsTest(ViewTestCase):
    def test_edit_priority(self) -> None:
        # Need extra power
        self.user.is_superuser = True
        self.user.save()

        source = self.get_unit().source_unit
        response = self.client.post(
            reverse("edit_context", kwargs={"pk": source.pk}),
            {"extra_flags": "priority:60"},
        )
        self.assertRedirects(response, source.get_absolute_url())

        unit = self.get_unit()
        self.assertEqual(unit.priority, 60)

    def test_edit_readonly(self) -> None:
        # Need extra power
        self.user.is_superuser = True
        self.user.save()

        unit = self.get_unit()
        old_state = unit.state
        source = unit.source_unit
        response = self.client.post(
            reverse("edit_context", kwargs={"pk": source.pk}),
            {"extra_flags": "read-only"},
        )
        self.assertRedirects(response, source.get_absolute_url())

        unit = self.get_unit()
        self.assertTrue(unit.readonly)
        self.assertNotEqual(unit.state, old_state)

        response = self.client.post(
            reverse("edit_context", kwargs={"pk": source.pk}), {"extra_flags": ""}
        )
        self.assertRedirects(response, source.get_absolute_url())

        unit = self.get_unit()
        self.assertFalse(unit.readonly)
        self.assertEqual(unit.state, old_state)

    def test_edit_context(self) -> None:
        # Need extra power
        self.user.is_superuser = True
        self.user.save()

        source = self.get_unit().source_unit
        response = self.client.post(
            reverse("edit_context", kwargs={"pk": source.pk}),
            {"explanation": "Extra context"},
        )
        self.assertRedirects(response, source.get_absolute_url())

        unit = self.get_unit().source_unit
        self.assertEqual(unit.context, "")
        self.assertEqual(unit.explanation, "Extra context")

    def test_edit_check_flags(self) -> None:
        # Need extra power
        self.user.is_superuser = True
        self.user.save()

        source = self.get_unit().source_unit
        response = self.client.post(
            reverse("edit_context", kwargs={"pk": source.pk}),
            {"extra_flags": "ignore-same"},
        )
        self.assertRedirects(response, source.get_absolute_url())

        unit = self.get_unit().source_unit
        self.assertEqual(unit.extra_flags, "ignore-same")

    def test_view_source(self) -> None:
        kwargs = {"path": [*self.component.get_url_path(), "en"]}
        response = self.client.get(reverse("show", kwargs=kwargs))
        self.assertContains(response, "Test/Test")

    def test_matrix(self) -> None:
        response = self.client.get(reverse("matrix", kwargs=self.kw_component))
        self.assertContains(response, "Czech")

    def test_matrix_load(self) -> None:
        response = self.client.get(
            reverse("matrix-load", kwargs=self.kw_component) + "?offset=0&lang=cs"
        )
        self.assertContains(response, 'lang="cs"')

    def test_toggle_flags(self) -> None:
        # Need extra power
        self.user.is_superuser = True
        self.user.save()

        source = self.get_unit().source_unit
        response = self.client.post(
            reverse("edit_context", kwargs={"pk": source.pk}), {"addflag": "read-only"}
        )
        self.assertRedirects(response, source.get_absolute_url())

        unit = self.get_unit()
        self.assertIn("read-only", unit.all_flags)

        source = self.get_unit().source_unit
        response = self.client.post(
            reverse("edit_context", kwargs={"pk": source.pk}), {"addflag": "read-only"}
        )
        self.assertRedirects(response, source.get_absolute_url())

        unit = self.get_unit()
        self.assertIn("read-only", unit.all_flags)

        response = self.client.post(
            reverse("edit_context", kwargs={"pk": source.pk}),
            {"removeflag": "read-only"},
        )
        self.assertRedirects(response, source.get_absolute_url())

        unit = self.get_unit()
        self.assertNotIn("read-only", unit.all_flags)
