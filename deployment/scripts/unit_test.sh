#!/bin/bash

python3 -m venv app-venv
. app-venv/bin/activate
pip install -U pip
pip install poetry tox
tox -r

exit $?
