from api import app

# For GCP Functions, you need a function called 'app' or 'main'
def main(request):
    # Use FastAPI's ASGI adapter for GCP Functions (Mangum or similar)
    from mangum import Mangum
    handler = Mangum(app)
    return handler(request)