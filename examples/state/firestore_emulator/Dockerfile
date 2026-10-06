# Google's Firestore emulator, as firebase-tools runs it, in an image of its own. Built by `docker compose up`
# unless FIRESTORE_IMAGE names an image you already have that contains `firebase` and Java.
FROM node:20-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openjdk-17-jre-headless \
 && rm -rf /var/lib/apt/lists/*

# The version whose export and restart-with-import this recipe was measured against.
RUN npm install -g firebase-tools@13.35.1 \
 && firebase setup:emulators:firestore

WORKDIR /recipe
