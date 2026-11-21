"""Minimal FastAPI + SQLAlchemy 2.0 async + rsqlx example.

Run:
    pip install fastapi uvicorn sqlalchemy-rsqlx
    uvicorn examples.fastapi_minimal:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = "sqlite+rsqlx:///rsqlx_fastapi_example.db"
engine = create_async_engine(DATABASE_URL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, body TEXT)")
        )
    yield
    await engine.dispose()


app = FastAPI(lifespan=lifespan)


class NoteIn(BaseModel):
    body: str


@app.post("/notes")
async def create_note(note: NoteIn):
    async with engine.begin() as conn:
        result = await conn.execute(
            text("INSERT INTO notes (body) VALUES (:b)"), {"b": note.body}
        )
        return {"id": result.lastrowid, "body": note.body}


@app.get("/notes")
async def list_notes():
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, body FROM notes ORDER BY id"))).all()
        return [{"id": row.id, "body": row.body} for row in rows]
