#!/usr/bin/env python3

# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Helper script to generate Python code for licenses.

See https://github.com/spdx/license-list-data
"""

import json

HEADER = '''# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
License data definitions.

This is an automatically generated file, see scripts/generate-license-data.py
"""

'''
TEMPLATE = """    (
        {0},
        {1},
        {2},
        {3},
    ),
"""


def escape_string(string: str) -> str:
    if '"' in string:
        if "'" in string:
            return repr(string)
        return f"'{string}'"
    return f'"{string}"'


# Some licenses are not classified as libre in SPDX
EXCEPTIONS = {
    # DFSG compatible, see https://wiki.debian.org/DFSGLicenses
    "CC-BY-3.0",
    "CC-BY-SA-3.0",
    # Public domain, see https://fedoraproject.org/wiki/Licensing/Beerware
    "Beerware",
}

with open("scripts/spdx-license-list/json/licenses.json") as handle:
    data = json.load(handle)

with open("weblate/utils/licensedata.py", "w") as output:
    output.write(HEADER)
    output.write("LICENSES = (\n")
    for item in sorted(data["licenses"], key=lambda x: x["name"].lower()):
        if item["isDeprecatedLicenseId"]:
            continue
        with open(
            f"scripts/spdx-license-list/json/details/{item['licenseId']}.json"
        ) as handle:
            details = json.load(handle)
        libre = (
            details.get("isFsfLibre", False)
            or details["isOsiApproved"]
            or item.get("isFsfLibre", False)
            or item["isOsiApproved"]
            or item["licenseId"] in EXCEPTIONS
        )
        output.write(
            TEMPLATE.format(
                escape_string(item["licenseId"]),
                escape_string(item["name"]),
                escape_string(item["reference"]),
                "True" if libre else "False",
            )
        )
    output.write(")\n")
