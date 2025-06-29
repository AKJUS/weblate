# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Generated by Django 5.2.1 on 2025-05-25 18:59

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("trans", "0036_alter_component_is_glossary"),
    ]

    operations = [
        migrations.AddField(
            model_name="component",
            name="contribute_project_tm",
            field=models.BooleanField(
                default=True,
                help_text="Allow this component's translations to be added to the project-level translation memory.",
                verbose_name="Contribute to project translation memory",
            ),
        ),
    ]
