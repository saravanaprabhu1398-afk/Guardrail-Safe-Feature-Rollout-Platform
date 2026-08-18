#!/bin/sh
set -eu

if [ -s /certs/public/ca.crt ] && \
    [ -s /certs/postgres/postgres.key ] && \
    [ -s /certs/redpanda/redpanda.key ] && \
    [ -s /certs/oidc/oidc.key ] && \
    [ -s /certs/gateway/gateway.key ]; then
    mkdir -p /evidence
    chown 10001:10001 /evidence
    exit 0
fi

umask 077

ca_key=/tmp/guardrail-local-production-ca.key

openssl genpkey -quiet -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "${ca_key}"
openssl req -quiet -x509 -new -sha256 -days 7 \
    -key "${ca_key}" \
    -subj "/CN=Guardrail Local Production CA" \
    -out /certs/public/ca.crt

issue_server_certificate() {
    name="$1"
    owner="$2"
    extra_dns="$3"
    target="/certs/${name}"

    cp /certs/public/ca.crt "${target}/ca.crt"
    openssl genpkey -quiet -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "${target}/${name}.key"
    openssl req -quiet -new -sha256 \
        -key "${target}/${name}.key" \
        -subj "/CN=${name}-prodtest" \
        -out "${target}/${name}.csr"
    printf '%s\n' \
        "subjectAltName=DNS:${name}-prodtest,DNS:localhost${extra_dns}" \
        "extendedKeyUsage=serverAuth" \
        > "${target}/${name}.ext"
    openssl x509 -req -sha256 -days 7 \
        -in "${target}/${name}.csr" \
        -CA /certs/public/ca.crt \
        -CAkey "${ca_key}" \
        -CAcreateserial \
        -extfile "${target}/${name}.ext" \
        -out "${target}/${name}.crt"
    chown "${owner}" "${target}/${name}.key" "${target}/${name}.crt"
    chmod 400 "${target}/${name}.key"
    chmod 444 "${target}/ca.crt" "${target}/${name}.crt"
    rm -f "${target}/${name}.csr" "${target}/${name}.ext"
}

issue_server_certificate postgres 70:70 ""
issue_server_certificate redpanda 101:101 ""
issue_server_certificate oidc 10001:10001 ""
issue_server_certificate gateway 0:0 ",DNS:gateway-prodtest"

chmod 444 /certs/public/ca.crt
rm -f "${ca_key}" /certs/public/ca.srl

mkdir -p /evidence
chown 10001:10001 /evidence
