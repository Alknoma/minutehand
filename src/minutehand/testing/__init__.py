"""For a pytest suite that uses `minutehand serve` in place of its emulators.

    from minutehand.testing.client import MinutehandClient, AsyncMinutehandClient
    from minutehand.testing.world import OpenWorld

The fixtures (`minutehand`, `minutehand_world`) come from the pytest plugin `minutehand.testing.plugin`,
which pytest loads by itself once `minutehand` is installed. It defines fixtures and nothing else, and imports
nothing of Minutehand until one is requested, so a suite that requests none is unchanged: this package
re-exports nothing for the same reason. See docs/serve.md.
"""
