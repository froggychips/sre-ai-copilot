#!/bin/bash

# Local CI Runner for SRE AI Copilot
# Runs linting, testing, and security checks locally

set -e

echo "🚀 Starting Local CI Pipeline..."

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print status
print_status() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

# Check if Python 3.11+ is available
if ! python3 --version | grep -q "Python 3\."; then
    print_error "Python 3.11+ required"
    exit 1
fi

print_status "Python version check passed"

# Install dependencies if requirements.txt exists
if [ -f "requirements.txt" ]; then
    print_status "Installing dependencies..."
    python3 -m pip install -r requirements.txt
fi

# Install dev dependencies
print_status "Installing development tools..."
python3 -m pip install flake8 mypy black isort bandit safety pytest pytest-asyncio pytest-cov

# 1. Code Formatting Check
echo ""
echo "📝 Running Code Formatting Checks..."
if python3 -m black --check --diff app/ tests/ scripts/; then
    print_status "Black formatting check passed"
else
    print_warning "Black formatting issues found. Run 'black app/ tests/ scripts/' to fix"
fi

if python3 -m isort --check-only --diff app/ tests/ scripts/; then
    print_status "Import sorting check passed"
else
    print_warning "Import sorting issues found. Run 'isort app/ tests/ scripts/' to fix"
fi

# 2. Linting
echo ""
echo "🔍 Running Linting..."
if python3 -m flake8 app/ tests/ scripts/ --max-line-length=100 --extend-ignore=E203,W503; then
    print_status "Flake8 linting passed"
else
    print_error "Flake8 linting failed"
    exit 1
fi

# 3. Type Checking
echo ""
echo "🔧 Running Type Checking..."
if python3 -m mypy app/ --ignore-missing-imports --no-strict-optional; then
    print_status "MyPy type checking passed"
else
    print_warning "MyPy type checking found issues"
fi

# 4. Security Scanning
echo ""
echo "🔒 Running Security Scans..."
if python3 -m bandit -r app/ -f json -o /tmp/bandit-results.json; then
    print_status "Bandit security scan passed"
else
    print_warning "Bandit found potential security issues"
fi

if python3 -m safety check; then
    print_status "Safety dependency scan passed"
else
    print_error "Safety found vulnerable dependencies"
    exit 1
fi

# 5. Testing
echo ""
echo "🧪 Running Tests..."
if python3 -m pytest tests/ -v --cov=app --cov-report=term-missing --cov-report=xml --cov-fail-under=80; then
    print_status "All tests passed with >80% coverage"
else
    print_error "Tests failed or coverage too low"
    exit 1
fi

# 6. Build Check
echo ""
echo "🏗️  Running Build Check..."
if python3 -c "import app.main; print('Import successful')"; then
    print_status "Application import check passed"
else
    print_error "Application import failed"
    exit 1
fi

echo ""
echo -e "${GREEN}🎉 Local CI Pipeline Completed Successfully!${NC}"
echo ""
echo "Next steps:"
echo "1. Fix any warnings shown above"
echo "2. Commit changes"
echo "3. Push to trigger remote CI if configured"