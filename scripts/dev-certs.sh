#!/bin/bash
# DEVELOPMENT/TEST ONLY: a throwaway local CA and a server certificate for localhost/127.0.0.1.
# For a real deployment use a certificate from your own CA or ACME (docs/operations/deployment.md).
set -euo pipefail
dir="${1:-certs}"
umask 077
mkdir -p "$dir"
if [ -e "$dir/server.crt" ]; then echo "certificates already exist in $dir"; exit 0; fi
openssl req -x509 -newkey rsa:3072 -nodes -days 30 -subj "/CN=memory-dev-ca" \
    -keyout "$dir/ca.key" -out "$dir/ca.crt" 2>/dev/null
openssl req -newkey rsa:3072 -nodes -subj "/CN=localhost" -keyout "$dir/server.key" -out "$dir/server.csr" 2>/dev/null
printf 'subjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=CA:FALSE\n' > "$dir/san.ext"
openssl x509 -req -in "$dir/server.csr" -CA "$dir/ca.crt" -CAkey "$dir/ca.key" -CAcreateserial -days 30 \
    -extfile "$dir/san.ext" -out "$dir/server.crt" 2>/dev/null
rm -f "$dir/server.csr" "$dir/san.ext" "$dir/ca.srl"
chmod 644 "$dir/server.crt" "$dir/ca.crt"; chmod 640 "$dir/server.key"
echo "created $dir/{ca.crt,server.crt,server.key} (30-day dev CA; never trust it outside tests)"
