"""sweep/hub/api/search.py -- the search verbs."""
from fastapi import APIRouter

router = APIRouter(prefix="/search", tags=["search"])
