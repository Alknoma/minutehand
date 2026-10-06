#!/bin/sh
# The emulator's command. Google's emulator reads an export only as it starts (`--import`), so a restore
# puts the snapshot at /exports/restore and restarts the container; this starts it from there when it exists.
set -e
cd /recipe
if [ -f /exports/restore/firebase-export-metadata.json ]; then
  exec firebase emulators:start --only firestore --project demo-minutehand --import /exports/restore
fi
exec firebase emulators:start --only firestore --project demo-minutehand
