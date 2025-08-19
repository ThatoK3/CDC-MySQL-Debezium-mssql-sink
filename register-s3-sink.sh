#!/bin/bash
set -e  # Exit on error

# Load environment variables
if [ -f .env ]; then
    set -a
    source .env
    set +a
else
    echo "Error: .env file not found!"
    exit 1
fi

# Check required environment variables
required_vars=("AWS_ACCESS_KEY_ID" "AWS_SECRET_ACCESS_KEY" "AWS_REGION" "S3_BUCKET" "SCHEMA_REGISTRY_URL")
for var in "${required_vars[@]}"; do
    if [ -z "${!var}" ]; then
        echo "Error: $var is not set in .env file!"
        exit 1
    fi
done

# Check if Connect REST API is available
if ! curl -f -s -o /dev/null http://localhost:8083/connectors/; then
    echo "Error: Kafka Connect REST API is not available at http://localhost:8083/"
    exit 1
fi

# Connector name
CONNECTOR_NAME="s3-sink-inventory"

# Delete existing connector if exists
if curl -f -s -o /dev/null http://localhost:8083/connectors/$CONNECTOR_NAME; then
    echo "Deleting existing connector: $CONNECTOR_NAME"
    curl -s -X DELETE http://localhost:8083/connectors/$CONNECTOR_NAME
    sleep 2
fi

echo "Registering Avro-based S3 Sink Connector..."

# Register S3 Sink with Avro
response=$(curl -s -o response.json -w "%{http_code}" -X POST http://localhost:8083/connectors/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "'"$CONNECTOR_NAME"'",
    "config": {
      "connector.class": "io.confluent.connect.s3.S3SinkConnector",
      "tasks.max": "1",
      "topics.dir": "topics",
      "topics": "dbserver1.inventory.customers,dbserver1.inventory.orders,dbserver1.inventory.products",

      "s3.bucket.name": "'"$S3_BUCKET"'",
      "s3.region": "'"$AWS_REGION"'",

      "aws.access.key.id": "'"$AWS_ACCESS_KEY_ID"'",
      "aws.secret.access.key": "'"$AWS_SECRET_ACCESS_KEY"'",

      "format.class": "io.confluent.connect.s3.format.avro.AvroFormat",
      "storage.class": "io.confluent.connect.s3.storage.S3Storage",
      "flush.size": "1000",
      "rotate.interval.ms": "600000",

      "schema.compatibility": "NONE",

      "key.converter": "io.confluent.connect.avro.AvroConverter",
      "key.converter.schema.registry.url": "'"$SCHEMA_REGISTRY_URL"'",
      "value.converter": "io.confluent.connect.avro.AvroConverter",
      "value.converter.schema.registry.url": "'"$SCHEMA_REGISTRY_URL"'"
    }
  }')

if [ "$response" -eq 201 ] || [ "$response" -eq 409 ]; then
    echo "✓ Successfully registered $CONNECTOR_NAME"
    cat response.json
    echo ""
else
    echo "✗ Failed to register $CONNECTOR_NAME (HTTP $response)"
    cat response.json
    echo ""
    rm -f response.json
    exit 1
fi

rm -f response.json

# Verify connector status
status=$(curl -s http://localhost:8083/connectors/$CONNECTOR_NAME/status | jq -r '.connector.state' 2>/dev/null || echo "UNKNOWN")
echo "Connector $CONNECTOR_NAME status: $status"

echo "Setup completed!"

