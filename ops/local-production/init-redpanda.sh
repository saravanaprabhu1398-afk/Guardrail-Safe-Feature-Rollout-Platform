#!/bin/sh
set -eu

admin_api() {
    rpk "$@" -X "admin.hosts=${REDPANDA_ADMIN_HOSTS}"
}

admin_kafka() {
    rpk "$@" \
        -X "brokers=${REDPANDA_BOOTSTRAP_SERVERS}" \
        -X tls.enabled=true \
        -X "tls.ca=${REDPANDA_CA_FILE}" \
        -X sasl.mechanism=SCRAM-SHA-256 \
        -X "user=${REDPANDA_ADMIN_USERNAME}" \
        -X "pass=${REDPANDA_ADMIN_PASSWORD}"
}

until admin_api security user list >/dev/null 2>&1; do
    sleep 1
done

if ! admin_api security user list | grep -q "${REDPANDA_ADMIN_USERNAME}"; then
    admin_api security user create "${REDPANDA_ADMIN_USERNAME}" \
        --password "${REDPANDA_ADMIN_PASSWORD}" \
        --mechanism scram-sha-256
fi

if ! admin_api security user list | grep -q "${REDPANDA_SERVICE_USERNAME}"; then
    admin_api security user create "${REDPANDA_SERVICE_USERNAME}" \
        --password "${REDPANDA_SERVICE_PASSWORD}" \
        --mechanism scram-sha-256
fi

if ! admin_kafka topic describe "${REDPANDA_TOPIC}" >/dev/null 2>&1; then
    admin_kafka topic create "${REDPANDA_TOPIC}" --partitions 3 --replicas 1
fi

admin_kafka security acl create \
    --allow-principal "User:${REDPANDA_SERVICE_USERNAME}" \
    --operation write,describe \
    --topic "${REDPANDA_TOPIC}" >/dev/null
admin_kafka security acl create \
    --allow-principal "User:${REDPANDA_SERVICE_USERNAME}" \
    --operation idempotent-write \
    --cluster >/dev/null

service_kafka_args="-X brokers=${REDPANDA_BOOTSTRAP_SERVERS} -X tls.enabled=true -X tls.ca=${REDPANDA_CA_FILE} -X sasl.mechanism=SCRAM-SHA-256 -X user=${REDPANDA_SERVICE_USERNAME} -X pass=${REDPANDA_SERVICE_PASSWORD}"
# `topic describe` also requests topic configs and therefore needs broader
# DescribeConfigs access. Listing proves the producer can authenticate and
# resolve the exact topic while keeping the runtime account least-privileged.
# shellcheck disable=SC2086
rpk topic list ${service_kafka_args} | awk -v topic="${REDPANDA_TOPIC}" '
    $1 == topic && $2 == 3 && $3 == 1 { found = 1 }
    END { exit !found }
'
