#!/bin/bash
set -e  # Exit on any error

# Load environment variables
if [ -f .env ]; then
    set -a
    source .env
    set +a
else
    echo "Error: .env file not found!"
    exit 1
fi

# Check if required environment variables are set
required_vars=("MSSQL_HOST" "MSSQL_PORT" "MSSQL_DB" "MSSQL_USER" "MSSQL_PASSWORD")
for var in "${required_vars[@]}"; do
    if [ -z "${!var}" ]; then
        echo "Error: $var is not set in .env file!"
        exit 1
    fi
done

# Check if Connect REST API is available
if ! curl -f -s -o /dev/null http://localhost:8083/connectors/; then
    echo "Error: Kafka Connect REST API is not available at http://localhost:8083/"
    echo "Please ensure Kafka Connect is running and accessible."
    exit 1
fi

# Delete existing connectors if they exist
connectors=("mssql-sink-orders" "mssql-sink-customers" "mssql-sink-products")
for connector in "${connectors[@]}"; do
    if curl -f -s -o /dev/null http://localhost:8083/connectors/$connector; then
        echo "Deleting existing connector: $connector"
        curl -s -X DELETE http://localhost:8083/connectors/$connector
        sleep 2  # Wait a bit for cleanup
    else
        echo "Connector $connector does not exist, skipping deletion"
    fi
done

echo "Registering MSSQL Sink Connectors with Avro..."

# Function to register connector with error checking
register_connector() {
    local name=$1
    local topic=$2
    local pk_field=$3
    
    echo "Registering connector: $name"
    
    local response=$(curl -s -o response.json -w "%{http_code}" -X POST http://localhost:8083/connectors/ \
      -H "Content-Type: application/json" \
      -d '{
        "name": "'"$name"'",
        "config": {
          "connector.class": "io.confluent.connect.jdbc.JdbcSinkConnector",
          "tasks.max": "1",
          "topics": "'"$topic"'",
          "connection.url": "jdbc:sqlserver://'"$MSSQL_HOST"':'"$MSSQL_PORT"';databaseName='"$MSSQL_DB"';encrypt=false;",
          "connection.user": "'"$MSSQL_USER"'",
          "connection.password": "'"$MSSQL_PASSWORD"'",
          "auto.create": "true",
          "auto.evolve": "true",
          "insert.mode": "upsert",
          "pk.mode": "record_key",
          "pk.fields": "'"$pk_field"'",

          "transforms": "unwrap,dropPrefix",
          "transforms.unwrap.type": "io.debezium.transforms.ExtractNewRecordState",
          "transforms.unwrap.drop.tombstones": "true",
          "transforms.dropPrefix.type": "org.apache.kafka.connect.transforms.RegexRouter",
          "transforms.dropPrefix.regex": "dbserver1\\.inventory\\.(.*)",
          "transforms.dropPrefix.replacement": "$1",

          "key.converter": "io.confluent.connect.avro.AvroConverter",
          "key.converter.schema.registry.url": "http://schema-registry:8081",
          "value.converter": "io.confluent.connect.avro.AvroConverter",
          "value.converter.schema.registry.url": "http://schema-registry:8081"
        }
      }')
    
    if [ "$response" -eq 201 ] || [ "$response" -eq 409 ]; then
        echo "✓ Successfully registered $name"
        cat response.json
        echo ""
    else
        echo "✗ Failed to register $name (HTTP $response)"
        cat response.json
        echo ""
        rm -f response.json
        exit 1
    fi
    
    rm -f response.json
    sleep 1  # Brief pause between registrations
}

# Register all connectors
register_connector "mssql-sink-orders" "dbserver1.inventory.orders" "order_number"
register_connector "mssql-sink-customers" "dbserver1.inventory.customers" "id"
register_connector "mssql-sink-products" "dbserver1.inventory.products" "id"

echo "All MSSQL sink connectors registered successfully!"

# Verify connectors are running
echo "Verifying connector status..."
for connector in "${connectors[@]}"; do
    status=$(curl -s http://localhost:8083/connectors/$connector/status | jq -r '.connector.state' 2>/dev/null || echo "UNKNOWN")
    echo "Connector $connector status: $status"
done

echo "Setup completed!"

