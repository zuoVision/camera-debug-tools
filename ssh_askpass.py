#!/usr/bin/env python3
"""OpenSSH askpass helper. The configured password is passed through the child environment."""

import os
import sys


sys.stdout.write(os.environ.get("CAMERA_DEBUG_SSH_PASSWORD", ""))
