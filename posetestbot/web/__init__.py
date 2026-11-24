"""Flask web UI package for PoseTestBot.

Deliberately imports nothing. Job workers import ``posetestbot.web.security``
for the approved run roots, and importing ``posetestbot.web.app`` builds the
default application and a job runner over the live job store.
"""
