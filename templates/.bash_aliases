#!/bin/bash

codegrep() {
  grep -n --color=always --exclude=jquery.js --exclude=*.po --exclude=*.min.js \
  --exclude=*.sql --exclude=*.min.css --exclude=*.map -H -I -R "$1" .
}

