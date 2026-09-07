"""sweep/hub/api/sample.py -- the sample verbs."""
from fastapi import APIRouter

router = APIRouter(prefix="/sample", tags=["sample"])
