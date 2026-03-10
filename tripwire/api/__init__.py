"""TripWire API layer."""

from fastapi import Request


def get_supabase(request: Request):
    """FastAPI dependency that returns the Supabase client from app state."""
    return request.app.state.supabase
