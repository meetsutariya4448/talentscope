from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from app.api import postings, analytics, qa
import os

app = FastAPI(title="TalentScope", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(postings.router, prefix="/postings", tags=["postings"])
app.include_router(analytics.router, prefix="/analytics", tags=["analytics"])
app.include_router(qa.router, prefix="/qa", tags=["qa"])


@app.get("/health")
def health():
    return {"status": "ok"}


# Serve dashboard
dashboard_path = os.path.join(os.path.dirname(__file__), "..", "dashboard")
if os.path.isdir(dashboard_path):
    app.mount("/dashboard", StaticFiles(directory=dashboard_path, html=True), name="dashboard")
