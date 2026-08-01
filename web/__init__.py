"""Local web front end. WEB_UI.md.

An adapter over `kernel.session`, exactly like `kernel/cli.py` is. Nothing in
here allocates, grades, or does state maths -- if this package ever needs to,
the seam was drawn in the wrong place.
"""
