#!/bin/bash
set -e

# Load environment variables
if [ -f .env ]; then
    set -a
    source .env
    set +a
else
    echo "Error: .env file not found!"
    exit 1
fi

# Check required vars
required_vars=("MSSQL_HOST" "MSSQL_PORT" "MSSQL_DB" "MSSQL_USER" "MSSQL_PASSWORD")
for var in "${required_vars[@]}"; do
    if [ -z "${!var}" ]; then
        echo "Error: $var is not set!"
        exit 1
    fi
done

# Delete existing connectors
connectors=("mssql-sink-orders" "mssql-sink-customers" "mssql-sink-products")
for connector in "${connectors[@]}"; do
    if curl -s -f http://localhost:8083/connectors/$connector >/dev/null; then
        echo "Deleting $connector"
        curl -s -X DELETE http://localhost:8083/connectors/$connector
        sleep 2
    fi
done

# Register connector function
register_connector() {
    local name=$1
    local topic=$2
    local pk_field=$3

    echo "Registering $name..."

    # Build JSON payload with jq to avoid shell escape issues
    payload=$(jq -n \
      --arg name "$name" \
      --arg topic "$topic" \
      --arg pk "$pk_field" \
      --arg host "$MSSQL_HOST" \
      --arg port "$MSSQL_PORT" \
      --arg db "$MSSQL_DB" \
      --arg user "$MSSQL_USER" \
      --arg pass "$MSSQL_PASSWORD" \
      '{
        name: $name,
        config: {
          "connector.class": "io.confluent.connect.jdbc.JdbcSinkConnector",
          "tasks.max": "1",
          topics: $topic,
          "connection.url": ("jdbc:sqlserver://" + $host + ":" + $port + ";databaseName=" + $db + ";encrypt=false;"),
          "connection.user": $user,
          "connection.password": $pass,
          "auto.create": "true",
          "auto.evolve": "true",
          "insert.mode": "upsert",
          "pk.mode": "record_key",
          "pk.fields": $pk,
          transforms: "unwrap,dropPrefix",
          "transforms.unwrap.type": "io.debezium.transforms.ExtractNewRecordState",
          "transforms.unwrap.drop.tombstones": "true",
          "transforms.dropPrefix.type": "org.apache.kafka.connect.transforms.RegexRouter",
          "transforms.dropPrefix.regex": "dbserver1\\.inventory\\.(.*)",
          "transforms.dropPrefix.replacement": "$1"
        }
      }')

    http_code=$(curl -s -o response.json -w "%{http_code}" -X POST \
      -H "Content-Type: application/json" \
      --data "$payload" \
      http://localhost:8083/connectors/)

    if [[ "$http_code" =~ 201|409 ]]; then
        echo "✓ $name registered"
        cat response.json
    else
        echo "✗ Failed $name (HTTP $http_code)"
        cat response.json
        rm -f response.json
        exit 1
    fi

    rm -f response.json
    sleep 1
}

# Register all connectors
register_connector "mssql-sink-orders" "dbserver1.inventory.orders" "order_number"
register_connector "mssql-sink-customers" "dbserver1.inventory.customers" "id"
register_connector "mssql-sink-products" "dbserver1.inventory.products" "id"

echo "All connectors registered!"

