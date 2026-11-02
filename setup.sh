#!/usr/bin/env bash
set -e
pip install --upgrade pip
# --only-binary: google-genai pulls pydantic-core, which is Rust. aarch64 wheels
# exist, but if pip ever falls back to an sdist it will try to compile inside the
# upload request and either take twenty minutes or fail after the browser has
# given up. Fail fast and loudly in install.log instead.
pip install --only-binary=:all: -r requirements.txt
