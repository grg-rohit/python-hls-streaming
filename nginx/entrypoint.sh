#!/bin/sh
set -eu
base=/etc/letsencrypt/live/rohitgrg.com.np
if [ -s "$base/fullchain.pem" ] && [ -s "$base/privkey.pem" ]; then
  cp /etc/nginx/hls/https.conf /etc/nginx/conf.d/default.conf
else
  cp /etc/nginx/hls/bootstrap.conf /etc/nginx/conf.d/default.conf
fi
