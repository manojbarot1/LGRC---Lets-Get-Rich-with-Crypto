#!/bin/bash
set -e

# LGRC — Let's Get Rich with Crypto
# Single-script deployment for macOS, Linux, Windows (WSL)

echo "🚀 LGRC Deployment Helper"
echo "========================="

# Check Docker
if ! command -v docker &> /dev/null; then
  echo "❌ Docker is not installed. Please install Docker from https://www.docker.com/products/docker-desktop"
  exit 1
fi

if ! command -v docker-compose &> /dev/null; then
  if ! docker compose version &> /dev/null; then
    echo "❌ Docker Compose is not installed or not accessible."
    exit 1
  fi
fi

echo "✓ Docker found"

# Set up project
PROJECT_DIR="${1:-.}"
cd "$PROJECT_DIR" || exit 1

echo "📁 Project directory: $PROJECT_DIR"

# Check .env
if [ ! -f .env ]; then
  echo "⚠️  .env not found. Creating from template..."
  if [ -f .env.example ]; then
    cp .env.example .env
    echo "✓ Created .env from .env.example"
  else
    echo "❌ .env.example not found"
    exit 1
  fi
fi

# Prompt for API key if not set
if ! grep -q "^ANTHROPIC_API_KEY=sk-" .env; then
  echo ""
  echo "📝 Please enter your Anthropic API key (starts with sk-ant-...):"
  read -r API_KEY
  if [ -z "$API_KEY" ]; then
    echo "❌ API key is required"
    exit 1
  fi
  sed -i.bak "s/ANTHROPIC_API_KEY=.*/ANTHROPIC_API_KEY=$API_KEY/" .env
  rm -f .env.bak
  echo "✓ API key saved to .env"
fi

# Clean database
echo ""
echo "🔄 Resetting database..."
rm -f data/sim.db 2>/dev/null || true
mkdir -p data
echo "✓ Database reset"

# Build & start
echo ""
echo "🔨 Building Docker image..."
docker compose build

echo ""
echo "🚀 Starting LGRC..."
docker compose up -d

# Wait for startup
echo "⏳ Waiting for startup..."
sleep 3

# Health check
echo "💚 Checking health..."
for i in {1..30}; do
  if curl -s http://localhost:8100/health > /dev/null 2>&1; then
    echo "✓ Service is healthy"
    break
  fi
  sleep 1
done

echo ""
echo "🎉 LGRC is running!"
echo "🌐 Open: http://localhost:8100"
echo ""
echo "📖 Controls:"
echo "  - View logs: docker compose logs -f"
echo "  - Stop:      docker compose down"
echo "  - Restart:   docker compose restart"
echo ""
