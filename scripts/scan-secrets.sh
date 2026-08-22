#!/usr/bin/env bash
#
# Scan tracked files for values that look like real personal data.
#
# CLAUDE.md documents this as a grep to run by hand before committing. This is
# the same patterns, with the placeholders CLAUDE.md declares safe masked out
# first, so anything that survives is worth a look rather than another
# 555-01xx fixture. That is what makes it usable as a gate instead of a check
# everyone learns to ignore.
#
# Only tracked files are scanned. docs/local/ is gitignored and is exactly
# where real hostnames and URLs are supposed to live, so it must stay out.

set -uo pipefail

# The patterns from CLAUDE.md: real-looking E.164 numbers, dashed phone
# numbers, consumer mail domains, and home directory paths naming a person.
PATTERN='\+1[0-9]{10}|[0-9]{3}-[0-9]{3}-[0-9]{4}|@(gmail|icloud|me)\.|/Users/[a-z]'

# Masking runs on the matched lines rather than on whole files, so a line
# carrying a placeholder *and* a real value still trips on the real one.
hits=$(
    git ls-files -z \
        | xargs -0 grep -nEiI "$PATTERN" 2>/dev/null \
        | sed -E \
            -e 's/\+1[0-9]{3}55501[0-9]{2}/(fiction-e164)/g' \
            -e 's/\(?[0-9]{3}\)?[-. ]?555[-. ]?01[0-9]{2}/(fiction-phone)/g' \
            -e 's#/Users/USERNAME#(placeholder-path)#g' \
        | grep -EiI "$PATTERN"
)

if [ -n "$hits" ]; then
    echo "Possible personal data in tracked files:"
    echo
    echo "$hits"
    echo
    echo "Every line above needs a look. If it is a placeholder this script"
    echo "does not know about yet, add it to the masks in $0."
    exit 1
fi

echo "No personal data found in tracked files."
