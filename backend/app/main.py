
from contextlib import asynccontextmanager
from pathlib import Path
import time
import uuid

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import select, func, text, or_, delete

from .config import settings
from .database import Base, engine, get_db
from .models import (
    User,
    Document,
    DocumentShare,
    Conversation,
    Message,
    Feedback,
    AuditEvent,
    UserMemory,
)
from .schemas import (
    RegisterRequest,
    LoginRequest,
    ChatRequest,
    FeedbackRequest,
)
from .security import (
    hash_password,
    verify_password,
    create_token,
    current_user,
    admin_user,
)
from .rag import (
    extract_text,
    index_document,
    retrieve,
    sha256,
    delete_document_chunks,
)
from .agent_graph import run_agent
from .agentic_monitoring import router as agentic_monitoring_router


# --------------------------------------------------
# Application startup
# --------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create missing tables, including document_shares.
    Base.metadata.create_all(bind=engine)

    # Backward-compatible PostgreSQL schema updates.
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE conversations "
            "ADD COLUMN IF NOT EXISTS folder_name VARCHAR(100)"
        ))
        conn.execute(text(
            "ALTER TABLE conversations "
            "ADD COLUMN IF NOT EXISTS is_pinned "
            "BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        conn.execute(text(
            "ALTER TABLE conversations "
            "ADD COLUMN IF NOT EXISTS is_archived "
            "BOOLEAN NOT NULL DEFAULT FALSE"
        ))

    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        item.strip()
        for item in settings.cors_origins.split(",")
        if item.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(agentic_monitoring_router)


# --------------------------------------------------
# Health check
# --------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "advanced-rag-api",
    }


# --------------------------------------------------
# Registration
# --------------------------------------------------

@app.post("/api/auth/register")
def register(
    body: RegisterRequest,
    db: Session = Depends(get_db),
):
    email = body.email.strip().lower()

    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(
            status_code=400,
            detail="Enter a valid email address",
        )

    existing_user = db.scalar(
        select(User).where(User.email == email)
    )

    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="Email already registered",
        )

    user = User(
        email=email,
        password_hash=hash_password(body.password),
        role="user",
        is_verified=False,
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    db.add(
        AuditEvent(
            actor_user_id=user.id,
            action="user.registered",
            details={"email": email},
        )
    )
    db.commit()

    return {
        "message": (
            "Account created. Email verification must be "
            "enabled before production use."
        )
    }


# --------------------------------------------------
# Login
# --------------------------------------------------

@app.post("/api/auth/login")
def login(
    body: LoginRequest,
    db: Session = Depends(get_db),
):
    email = body.email.strip().lower()

    user = db.scalar(
        select(User).where(User.email == email)
    )

    if not user or not verify_password(
        body.password,
        user.password_hash,
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password",
        )

    return {
        "access_token": create_token(user),
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "role": user.role,
        },
    }


# --------------------------------------------------
# Current user
# --------------------------------------------------

@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
    }


# --------------------------------------------------
# Upload and index documents
# --------------------------------------------------

@app.post("/api/documents")
async def upload_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(admin_user),
):
    name = Path(file.filename or "upload.txt").name
    ext = Path(name).suffix.lower()

    if ext not in {".pdf", ".txt", ".docx"}:
        raise HTTPException(
            status_code=415,
            detail="Only PDF, TXT and DOCX files are supported",
        )

    data = await file.read()

    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds {settings.max_upload_mb} MB limit",
        )

    document_id = str(uuid.uuid4())

    row = Document(
        id=document_id,
        owner_user_id=user.id,
        filename=name,
        checksum=sha256(data),
        status="processing",
    )

    db.add(row)
    db.commit()

    try:
        extracted_text = extract_text(name, data)

        count = index_document(
            document_id=document_id,
            owner_user_id=user.id,
            filename=name,
            text=extracted_text,
        )

        row.status = "ready"
        row.chunk_count = count

        db.add(
            AuditEvent(
                actor_user_id=user.id,
                action="document.indexed",
                details={
                    "document_id": document_id,
                    "chunks": count,
                },
            )
        )

        db.commit()

        return {
            "document_id": document_id,
            "filename": name,
            "status": row.status,
            "chunk_count": count,
        }

    except Exception as exc:
        db.rollback()

        failed_row = db.get(Document, document_id)
        if failed_row:
            failed_row.status = "failed"
            db.commit()

        raise HTTPException(
            status_code=400,
            detail=f"Document ingestion failed: {str(exc)[:250]}",
        ) from exc


# --------------------------------------------------
# List owned and explicitly shared documents
# --------------------------------------------------

@app.get("/api/documents")
def list_documents(
    db: Session = Depends(get_db),
    user: User = Depends(admin_user),
):
    shared_ids = (
        select(DocumentShare.document_id)
        .where(
            DocumentShare.shared_with_user_id == user.id
        )
    )

    rows = db.scalars(
        select(Document)
        .where(
            or_(
                Document.owner_user_id == user.id,
                Document.id.in_(shared_ids),
            )
        )
        .order_by(Document.created_at.desc())
    ).all()

    return [
        {
            "id": row.id,
            "filename": row.filename,
            "status": row.status,
            "chunk_count": row.chunk_count,
            "created_at": (
                row.created_at.isoformat()
                if row.created_at else None
            ),
            "is_owner": row.owner_user_id == user.id,
        }
        for row in rows
    ]


# --------------------------------------------------
# Admin: list all documents for management
# --------------------------------------------------

@app.get("/api/admin/documents")
def admin_list_documents(
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
):
    rows = db.execute(
        select(Document, User.email)
        .join(User, Document.owner_user_id == User.id)
        .order_by(Document.created_at.desc())
    ).all()

    return [
        {
            "id": document.id,
            "filename": document.filename,
            "status": document.status,
            "chunk_count": document.chunk_count,
            "created_at": (
                document.created_at.isoformat()
                if document.created_at else None
            ),
            "owner_user_id": document.owner_user_id,
            "owner_email": owner_email,
        }
        for document, owner_email in rows
    ]



# --------------------------------------------------
# Admin: grant document access
# --------------------------------------------------

@app.post("/api/admin/documents/{document_id}/shares")
def grant_document_share(
    document_id: str,
    body: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
):
    target_user_id = body.get("user_id")

    # bool is a subclass of int in Python, so reject it explicitly.
    if (
        not isinstance(target_user_id, int)
        or isinstance(target_user_id, bool)
        or target_user_id <= 0
    ):
        raise HTTPException(
            status_code=422,
            detail="user_id must be a positive integer",
        )

    document = db.get(Document, document_id)

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    target_user = db.get(User, target_user_id)

    if not target_user:
        raise HTTPException(
            status_code=404,
            detail="Target user not found",
        )

    existing_share = db.scalar(
        select(DocumentShare).where(
            DocumentShare.document_id == document_id,
            DocumentShare.shared_with_user_id == target_user_id,
        )
    )

    if existing_share:
        return {
            "status": "already_shared",
            "document_id": document_id,
            "user_id": target_user_id,
        }

    share = DocumentShare(
        document_id=document_id,
        shared_with_user_id=target_user_id,
        granted_by_user_id=admin.id,
    )

    db.add(share)
    db.add(
        AuditEvent(
            actor_user_id=admin.id,
            action="document.share.granted",
            details={
                "document_id": document_id,
                "target_user_id": target_user_id,
            },
        )
    )

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "Could not create the share. It may already exist; "
                "check the document's current permissions."
            ),
        ) from exc

    return {
        "status": "shared",
        "document_id": document_id,
        "user_id": target_user_id,
    }


# --------------------------------------------------
# Admin: revoke document access
# --------------------------------------------------

@app.delete(
    "/api/admin/documents/{document_id}/shares/{target_user_id}"
)
def revoke_document_share(
    document_id: str,
    target_user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
):
    share = db.scalar(
        select(DocumentShare).where(
            DocumentShare.document_id == document_id,
            DocumentShare.shared_with_user_id == target_user_id,
        )
    )

    if not share:
        raise HTTPException(
            status_code=404,
            detail="Document share not found",
        )

    db.delete(share)

    db.add(
        AuditEvent(
            actor_user_id=admin.id,
            action="document.share.revoked",
            details={
                "document_id": document_id,
                "target_user_id": target_user_id,
            },
        )
    )

    db.commit()

    return {
        "status": "revoked",
        "document_id": document_id,
        "user_id": target_user_id,
    }


# --------------------------------------------------
# Admin: delete a document and its indexed chunks
# --------------------------------------------------

@app.delete("/api/admin/documents/{document_id}")
def delete_document(
    document_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
):
    document = db.get(Document, document_id)

    if document is None:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    # Remove the selected document's ChromaDB chunks first.
    try:
        delete_document_chunks(document_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not delete document chunks; database records were preserved.",
        ) from exc

    try:
        # Explicitly remove access grants for this document.
        db.execute(
            delete(DocumentShare).where(
                DocumentShare.document_id == document_id
            )
        )

        filename = document.filename

        db.add(
            AuditEvent(
                actor_user_id=admin.id,
                action="document.deleted",
                details={
                    "document_id": document_id,
                    "filename": filename,
                },
            )
        )

        db.delete(document)
        db.commit()

    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=(
                "ChromaDB chunks were deleted, but database cleanup failed. "
                "Check the database before retrying."
            ),
        ) from exc

    return {
        "status": "deleted",
        "document_id": document_id,
        "filename": filename,
    }



# --------------------------------------------------
# Chat using MCP retrieval and LLM
# --------------------------------------------------

@app.post("/api/chat")
async def chat(
    body: ChatRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    start = time.perf_counter()

    if not body.question.strip():
        raise HTTPException(
            status_code=422,
            detail="Question cannot be empty",
        )

    if body.conversation_id:
        conv = db.get(Conversation, body.conversation_id)

        if not conv or conv.user_id != user.id:
            raise HTTPException(
                status_code=404,
                detail="Conversation not found",
            )
    else:
        conv = Conversation(
            id=str(uuid.uuid4()),
            user_id=user.id,
            title=body.question[:80],
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)

    user_msg = Message(
        conversation_id=conv.id,
        role="user",
        content=body.question,
        sources=[],
    )

    db.add(user_msg)
    db.commit()

    try:
        # Load the previous conversation messages, excluding this question.
        history_rows = db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conv.id,
                Message.id != user_msg.id,
                Message.role.in_(["user", "assistant"]),
            )
            .order_by(Message.id.desc())
            .limit(10)
        ).all()

        conversation_history = [
            {
                "role": message.role,
                "content": message.content[:3000],
            }
            for message in reversed(history_rows)
        ]

        saved_memory = db.get(UserMemory, user.id)

        agent_result = await run_agent(
            question=body.question,
            trusted_user_id=user.id,
            memory=saved_memory.content if saved_memory else "",
            history=conversation_history,
        )

        contexts = agent_result["contexts"]
        agent_timings = agent_result.get("timings_ms", {})
        response = agent_result["answer"]

        sources = []
        for item in contexts:
            metadata = item.get("metadata") or {}
            source_type = metadata.get("source_type", "document")
            sources.append({
                "source_type": source_type,
                "filename": item.get("filename") or metadata.get("filename")
                    or metadata.get("title") or "Source",
                "title": metadata.get("title"),
                "url": metadata.get("url") if source_type == "web" else None,
                "chunk_index": item.get("chunk_index")
                    if "chunk_index" in item else metadata.get("chunk_index"),
                "distance": item.get("distance"),
                "document_id": item.get("document_id")
                    or metadata.get("document_id"),
            })

        assistant_msg = Message(
            conversation_id=conv.id,
            role="assistant",
            content=response,
            sources=sources,
        )

        db.add(assistant_msg)

        latency_ms = round(
            (time.perf_counter() - start) * 1000
        )

        db.add(
            AuditEvent(
                actor_user_id=user.id,
                action="chat.completed",
                details={
                    "conversation_id": conv.id,
                    "retrieval_count": len(contexts),
                    "latency_ms": latency_ms,
                },
            )
        )

        db.commit()
        db.refresh(assistant_msg)

        return {
            "conversation_id": conv.id,
            "message_id": assistant_msg.id,
            "answer": response,
            "sources": sources,
            "retrieval_count": len(contexts),
            "latency_ms": round(
                (time.perf_counter() - start) * 1000, 2
            ),
            "timings_ms": {
                "retrieval": agent_timings.get("retrieval", 0.0),
                "llm": agent_timings.get("llm", 0.0),
                "total": round(
                    (time.perf_counter() - start) * 1000, 2
                ),
            },
        }

    except HTTPException:
        db.rollback()
        raise

    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=502,
            detail=f"Chat request failed: {str(exc)[:250]}",
        ) from exc


# --------------------------------------------------
# List conversations
# --------------------------------------------------

@app.get("/api/conversations")
def list_conversations(
    search: str = "",
    include_archived: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    query = select(Conversation).where(
        Conversation.user_id == user.id
    )

    if not include_archived:
        query = query.where(
            Conversation.is_archived.is_(False)
        )

    term = search.strip()

    if term:
        pattern = f"%{term[:120]}%"

        matching_messages = select(
            Message.conversation_id
        ).where(
            Message.content.ilike(pattern)
        )

        query = query.where(
            or_(
                Conversation.title.ilike(pattern),
                Conversation.folder_name.ilike(pattern),
                Conversation.id.in_(matching_messages),
            )
        )

    rows = db.scalars(
        query.order_by(
            Conversation.is_pinned.desc(),
            Conversation.created_at.desc(),
        )
    ).all()

    return [
        {
            "id": row.id,
            "title": row.title,
            "folder_name": row.folder_name,
            "is_pinned": row.is_pinned,
            "is_archived": row.is_archived,
            "created_at": (
                row.created_at.isoformat()
                if row.created_at else None
            ),
        }
        for row in rows
    ]


# --------------------------------------------------
# Update conversation
# --------------------------------------------------

@app.patch("/api/conversations/{conversation_id}")
def update_conversation(
    conversation_id: str,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    conv = db.get(Conversation, conversation_id)

    if not conv or conv.user_id != user.id:
        raise HTTPException(
            status_code=404,
            detail="Conversation not found",
        )

    allowed = {
        "title",
        "folder_name",
        "is_pinned",
        "is_archived",
    }

    if not body or any(key not in allowed for key in body):
        raise HTTPException(
            status_code=422,
            detail=(
                "Provide title, folder_name, is_pinned, "
                "or is_archived"
            ),
        )

    if "title" in body:
        title = body["title"]

        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title.strip()) > 255
        ):
            raise HTTPException(
                status_code=422,
                detail="Title must contain 1–255 characters",
            )

        conv.title = title.strip()

    if "folder_name" in body:
        folder = body["folder_name"]

        if (
            folder is not None
            and (
                not isinstance(folder, str)
                or len(folder.strip()) > 100
            )
        ):
            raise HTTPException(
                status_code=422,
                detail="Folder name must be 100 characters or fewer",
            )

        conv.folder_name = (
            folder.strip()
            if isinstance(folder, str) and folder.strip()
            else None
        )

    for key in ("is_pinned", "is_archived"):
        if key in body:
            if not isinstance(body[key], bool):
                raise HTTPException(
                    status_code=422,
                    detail=f"{key} must be true or false",
                )

            setattr(conv, key, body[key])

    db.add(
        AuditEvent(
            actor_user_id=user.id,
            action="conversation.updated",
            details={
                "conversation_id": conversation_id,
                "fields": list(body.keys()),
            },
        )
    )

    db.commit()
    db.refresh(conv)

    return {
        "id": conv.id,
        "title": conv.title,
        "folder_name": conv.folder_name,
        "is_pinned": conv.is_pinned,
        "is_archived": conv.is_archived,
    }


# --------------------------------------------------
# Delete conversation and its messages/feedback
# --------------------------------------------------

@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    conv = db.get(Conversation, conversation_id)

    if not conv or conv.user_id != user.id:
        raise HTTPException(
            status_code=404,
            detail="Conversation not found",
        )

    message_ids = select(Message.id).where(
        Message.conversation_id == conversation_id
    )

    db.execute(
        delete(Feedback).where(
            Feedback.message_id.in_(message_ids)
        )
    )

    db.execute(
        delete(Message).where(
            Message.conversation_id == conversation_id
        )
    )

    db.add(
        AuditEvent(
            actor_user_id=user.id,
            action="conversation.deleted",
            details={"conversation_id": conversation_id},
        )
    )

    db.delete(conv)
    db.commit()

    return {
        "status": "deleted",
        "conversation_id": conversation_id,
    }


# --------------------------------------------------
# Get conversation messages
# --------------------------------------------------

@app.get("/api/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    conv = db.get(Conversation, conversation_id)

    if not conv or conv.user_id != user.id:
        raise HTTPException(
            status_code=404,
            detail="Conversation not found",
        )

    rows = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id)
    ).all()

    return [
        {
            "id": row.id,
            "role": row.role,
            "content": row.content,
            "sources": row.sources,
        }
        for row in rows
    ]


# --------------------------------------------------
# Submit feedback
# --------------------------------------------------

@app.post("/api/feedback")
def feedback(
    body: FeedbackRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    msg = db.get(Message, body.message_id)

    if not msg:
        raise HTTPException(
            status_code=404,
            detail="Message not found",
        )

    conv = db.get(Conversation, msg.conversation_id)

    if not conv or conv.user_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="Cannot rate another user's message",
        )

    db.add(
        Feedback(
            user_id=user.id,
            message_id=body.message_id,
            rating=body.rating,
            comment=body.comment,
        )
    )

    db.add(
        AuditEvent(
            actor_user_id=user.id,
            action="feedback.submitted",
            details={
                "message_id": body.message_id,
                "rating": body.rating,
            },
        )
    )

    db.commit()

    return {"status": "saved"}


# --------------------------------------------------
# Admin metrics
# --------------------------------------------------

@app.get("/api/admin/metrics")
def admin_metrics(
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
):
    return {
        "users": (
            db.scalar(select(func.count()).select_from(User)) or 0
        ),
        "documents": (
            db.scalar(select(func.count()).select_from(Document)) or 0
        ),
        "ready_documents": (
            db.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.status == "ready")
            ) or 0
        ),
        "chunks_indexed": (
            db.scalar(
                select(func.coalesce(func.sum(Document.chunk_count), 0))
            ) or 0
        ),
        "messages": (
            db.scalar(select(func.count()).select_from(Message)) or 0
        ),
        "feedback_items": (
            db.scalar(select(func.count()).select_from(Feedback)) or 0
        ),
        "failed_documents": (
            db.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.status == "failed")
            ) or 0
        ),
    }


# --------------------------------------------------
# Admin audit history
# --------------------------------------------------

@app.get("/api/admin/audit")
def admin_audit(
    limit: int = 100,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
):
    limit = max(1, min(limit, 500))

    rows = db.scalars(
        select(AuditEvent)
        .order_by(AuditEvent.created_at.desc())
        .limit(limit)
    ).all()

    return [
        {
            "id": row.id,
            "actor_user_id": row.actor_user_id,
            "action": row.action,
            "details": row.details,
            "created_at": (
                row.created_at.isoformat()
                if row.created_at else None
            ),
        }
        for row in rows
    ]


# --------------------------------------------------
# User memory
# --------------------------------------------------

@app.get("/api/memory")
def get_memory(
    db: Session = Depends(get_db),
    user: User = Depends(admin_user),
):
    row = db.get(UserMemory, user.id)

    return {
        "content": row.content if row else "",
        "updated_at": (
            row.updated_at.isoformat()
            if row and row.updated_at else None
        ),
    }


@app.put("/api/memory")
def save_memory(
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(admin_user),
):
    content = body.get("content", "")

    if not isinstance(content, str):
        raise HTTPException(
            status_code=422,
            detail="content must be text",
        )

    if len(content) > 5000:
        raise HTTPException(
            status_code=413,
            detail="Memory must be 5,000 characters or fewer",
        )

    row = db.get(UserMemory, user.id)

    if row is None:
        row = UserMemory(
            user_id=user.id,
            content=content.strip(),
        )
        db.add(row)
    else:
        row.content = content.strip()

    db.add(
        AuditEvent(
            actor_user_id=user.id,
            action="memory.updated",
            details={"characters": len(content.strip())},
        )
    )

    db.commit()
    db.refresh(row)

    return {
        "content": row.content,
        "updated_at": (
            row.updated_at.isoformat()
            if row.updated_at else None
        ),
    }


# --------------------------------------------------
# Admin workspace: users, application traces and safe settings
# --------------------------------------------------

@app.get("/api/admin/users")
def admin_list_users(
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
):
    rows = db.scalars(select(User).order_by(User.created_at.desc())).all()
    return [
        {
            "id": row.id,
            "email": row.email,
            "role": row.role,
            "is_verified": row.is_verified,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


@app.patch("/api/admin/users/{user_id}/role")
def admin_update_user_role(
    user_id: int,
    body: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
):
    role = body.get("role")
    if role not in {"admin", "user"}:
        raise HTTPException(status_code=422, detail="Role must be 'admin' or 'user'")
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == admin.id and role != "admin":
        raise HTTPException(status_code=400, detail="You cannot remove your own administrator role")
    if target.role == "admin" and role != "admin":
        admin_count = db.scalar(select(func.count()).select_from(User).where(User.role == "admin")) or 0
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="At least one administrator account must remain")
    previous_role = target.role
    target.role = role
    db.add(AuditEvent(
        actor_user_id=admin.id,
        action="admin.user_role_updated",
        details={"target_user_id": target.id, "previous_role": previous_role, "new_role": role},
    ))
    db.commit()
    return {"id": target.id, "email": target.email, "role": target.role}


@app.get("/api/admin/conversations")
def admin_list_conversations(
    limit: int = 200,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
):
    limit = max(1, min(limit, 1000))
    rows = db.execute(
        select(Conversation, User.email)
        .join(User, Conversation.user_id == User.id)
        .order_by(Conversation.created_at.desc())
        .limit(limit)
    ).all()
    output = []
    for conversation, email in rows:
        count = db.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == conversation.id)) or 0
        output.append({
            "id": conversation.id,
            "user_id": conversation.user_id,
            "user_email": email,
            "title": conversation.title,
            "message_count": count,
            "created_at": conversation.created_at.isoformat() if conversation.created_at else None,
            "is_archived": conversation.is_archived,
        })
    return output


@app.get("/api/admin/conversations/{conversation_id}")
def admin_get_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
):
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    owner = db.get(User, conversation.user_id)
    messages = db.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id.asc())
    ).all()
    return {
        "id": conversation.id,
        "title": conversation.title,
        "user_email": owner.email if owner else "Unknown",
        "messages": [
            {"id": m.id, "role": m.role, "content": m.content, "sources": m.sources or [], "created_at": m.created_at.isoformat() if m.created_at else None}
            for m in messages
        ],
    }


@app.delete("/api/admin/conversations/{conversation_id}")
def admin_delete_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
):
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    message_ids = select(Message.id).where(Message.conversation_id == conversation_id)
    db.execute(delete(Feedback).where(Feedback.message_id.in_(message_ids)))
    db.execute(delete(Message).where(Message.conversation_id == conversation_id))
    db.add(AuditEvent(actor_user_id=admin.id, action="admin.conversation_deleted", details={"conversation_id": conversation_id, "owner_user_id": conversation.user_id}))
    db.delete(conversation)
    db.commit()
    return {"status": "deleted", "conversation_id": conversation_id}


@app.get("/api/admin/traces")
def admin_list_application_traces(
    limit: int = 100,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
):
    """Return persisted chat completion audit events, not fabricated Langfuse traces."""
    limit = max(1, min(limit, 500))
    rows = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.action == "chat.completed")
        .order_by(AuditEvent.created_at.desc())
        .limit(limit)
    ).all()
    output = []
    for row in rows:
        details = row.details or {}
        conversation_id = details.get("conversation_id")
        conversation = db.get(Conversation, conversation_id) if conversation_id else None
        output.append({
            "id": row.id,
            "action": row.action,
            "query": conversation.title if conversation else "Conversation no longer available",
            "conversation_id": conversation_id,
            "status": "completed",
            "latency_ms": details.get("latency_ms"),
            "retrieval_count": details.get("retrieval_count"),
            "actor_user_id": row.actor_user_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })
    return output



@app.get("/api/admin/langfuse/observations")
def admin_langfuse_observations(
    limit: int = 50,
    admin: User = Depends(admin_user),
):
    """Return safe operational fields from Langfuse observations."""
    limit = max(1, min(limit, 100))

    try:
        from langfuse import get_client

        client = get_client()
        result = client.api.observations.get_many(
            limit=limit,
            fields="core,basic,time,metadata",
        )

        observations = []

        for obs in (result.data or []):
            start = getattr(obs, "start_time", None)
            end = getattr(obs, "end_time", None)

            duration_ms = None
            if start is not None and end is not None:
                duration_ms = round(
                    (end - start).total_seconds() * 1000, 2
                )

            metadata = getattr(obs, "metadata", None) or {}
            if not isinstance(metadata, dict):
                metadata = {}

            safe_metadata = {}
            for key in (
                "status", "retrieval_count", "retrieval_error",
                "evidence_found", "attempts",
            ):
                value = metadata.get(key)
                if isinstance(value, (str, int, float, bool)):
                    safe_metadata[key] = value

            timings = metadata.get("timings_ms")
            if isinstance(timings, dict):
                safe_metadata["timings_ms"] = {
                    str(k): v for k, v in timings.items()
                    if isinstance(v, (int, float))
                    and not isinstance(v, bool)
                }

            observations.append({
                "id": getattr(obs, "id", None),
                "trace_id": getattr(obs, "trace_id", None),
                "parent_observation_id": getattr(
                    obs, "parent_observation_id", None
                ),
                "name": getattr(obs, "name", None),
                "type": str(getattr(obs, "type", "") or ""),
                "level": str(getattr(obs, "level", "") or ""),
                "model": getattr(obs, "model", None),
                "is_root_observation": getattr(
                    obs, "is_root_observation", None
                ),
                "start_time": start.isoformat() if start else None,
                "end_time": end.isoformat() if end else None,
                "duration_ms": duration_ms,
                "metadata": safe_metadata,
            })

        return {
            "available": True,
            "count": len(observations),
            "observations": observations,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not retrieve Langfuse observations. "
                   "Check the connection and API permissions.",
        ) from exc



@app.get("/api/admin/langfuse/observations/export.csv")
def admin_export_langfuse_observations(
    start_date: str,
    end_date: str,
    admin: User = Depends(admin_user),
):
    """Export safe Langfuse observation fields to a date-filtered CSV."""
    import csv
    import io
    from datetime import date, datetime, time, timedelta, timezone
    from fastapi.responses import Response

    try:
        first_day = date.fromisoformat(start_date)
        last_day = date.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Dates must use YYYY-MM-DD format.",
        ) from exc

    if last_day < first_day:
        raise HTTPException(
            status_code=400,
            detail="end_date must be on or after start_date.",
        )

    start_dt = datetime.combine(
        first_day, time.min, tzinfo=timezone.utc
    )
    end_dt = datetime.combine(
        last_day + timedelta(days=1),
        time.min,
        tzinfo=timezone.utc,
    )

    columns = [
        "trace_id", "observation_id", "parent_observation_id",
        "name", "type", "level", "model",
        "start_time", "end_time", "duration_ms",
        "status", "retrieval_count", "evidence_found", "attempts",
        "retrieval_ms", "llm_ms",
    ]

    try:
        from langfuse import get_client

        client = get_client()
        output = io.StringIO(newline="")
        writer = csv.DictWriter(
            output, fieldnames=columns, extrasaction="ignore"
        )
        writer.writeheader()

        cursor = None
        seen_cursors = set()
        exported = 0
        max_rows = 100000

        while True:
            result = client.api.observations.get_many(
                limit=1000,
                fields="core,basic,time,metadata",
                from_start_time=start_dt,
                to_start_time=end_dt,
                **({"cursor": cursor} if cursor else {}),
            )

            for obs in (result.data or []):
                start = getattr(obs, "start_time", None)
                end = getattr(obs, "end_time", None)
                duration_ms = None

                if start is not None and end is not None:
                    duration_ms = round(
                        (end - start).total_seconds() * 1000, 2
                    )

                metadata = getattr(obs, "metadata", None) or {}
                if not isinstance(metadata, dict):
                    metadata = {}

                timings = metadata.get("timings_ms")
                if not isinstance(timings, dict):
                    timings = {}

                def scalar(value):
                    if isinstance(value, (str, int, float, bool)):
                        return value
                    return ""

                writer.writerow({
                    "trace_id": getattr(obs, "trace_id", None),
                    "observation_id": getattr(obs, "id", None),
                    "parent_observation_id": getattr(
                        obs, "parent_observation_id", None
                    ),
                    "name": getattr(obs, "name", None),
                    "type": getattr(obs, "type", None),
                    "level": getattr(obs, "level", None),
                    "model": getattr(obs, "model", None),
                    "start_time": start.isoformat() if start else "",
                    "end_time": end.isoformat() if end else "",
                    "duration_ms": duration_ms,
                    "status": scalar(metadata.get("status")),
                    "retrieval_count": scalar(
                        metadata.get("retrieval_count")
                    ),
                    "evidence_found": scalar(
                        metadata.get("evidence_found")
                    ),
                    "attempts": scalar(metadata.get("attempts")),
                    "retrieval_ms": scalar(timings.get("retrieval")),
                    "llm_ms": scalar(timings.get("llm")),
                })

                exported += 1
                if exported >= max_rows:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            "Export exceeded 100,000 observations. "
                            "Choose a shorter date range."
                        ),
                    )

            meta = getattr(result, "meta", None)
            next_cursor = (
                meta.get("cursor") if isinstance(meta, dict)
                else getattr(meta, "cursor", None)
            )

            if not next_cursor or next_cursor in seen_cursors:
                break

            seen_cursors.add(next_cursor)
            cursor = next_cursor

        filename = (
            f"langfuse-observations-{first_day.isoformat()}-"
            f"to-{last_day.isoformat()}.csv"
        )

        return Response(
            content="\ufeff" + output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition":
                    f'attachment; filename="{filename}"'
            },
        )

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Langfuse CSV export failed. Check Langfuse "
                "connectivity, permissions, and API logs."
            ),
        ) from exc


@app.get("/api/admin/pipeline/metrics")
def admin_pipeline_metrics(
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
):
    rows = db.scalars(select(Document)).all()
    ready = sum(1 for row in rows if row.status == "ready")
    processing = sum(1 for row in rows if row.status == "processing")
    failed = sum(1 for row in rows if row.status == "failed")
    chunks = sum((row.chunk_count or 0) for row in rows if row.status == "ready")
    return {
        "documents_total": len(rows),
        "documents_ready": ready,
        "documents_processing": processing,
        "documents_failed": failed,
        "chunks_indexed": chunks,
        "stages": [
            {"name": "Document upload", "status": "complete" if rows else "waiting", "detail": f"{len(rows)} document record(s)"},
            {"name": "Text extraction", "status": "error" if failed else "complete" if ready else "waiting", "detail": f"{ready} ready · {failed} failed · {processing} processing"},
            {"name": "Chunking", "status": "complete" if chunks else "waiting", "detail": f"{chunks} chunks recorded"},
            {"name": "Embeddings + ChromaDB", "status": "complete" if ready else "waiting", "detail": "Ingestion status inferred from ready document records"},
        ],
    }


@app.get("/api/admin/settings")
def admin_safe_settings(
    admin: User = Depends(admin_user),
):
    """Expose safe, non-secret configuration values only."""
    return {
        "environment": settings.environment,
        "embedding_model": settings.embedding_model,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "max_upload_mb": settings.max_upload_mb,
        "jwt_expire_minutes": settings.jwt_expire_minutes,
        "chroma_path_configured": bool(settings.chroma_path),
        "database_configured": bool(settings.database_url),
        "llm_credentials_configured": bool(settings.groq_api_key),
        "langfuse_instrumentation": "not confirmed by this settings endpoint",
    }


# --------------------------------------------------
# Pipeline metrics
# --------------------------------------------------

@app.get("/api/pipeline/metrics")
def pipeline_metrics(
    db: Session = Depends(get_db),
    user: User = Depends(admin_user),
):
    rows = db.scalars(
        select(Document).where(
            Document.owner_user_id == user.id
        )
    ).all()

    statuses = {
        "ready": 0,
        "processing": 0,
        "failed": 0,
    }

    for row in rows:
        status = (
            row.status
            if row.status in statuses
            else "processing"
        )
        statuses[status] += 1

    total_chunks = sum(
        row.chunk_count or 0 for row in rows
    )

    ready_chunks = sum(
        row.chunk_count or 0
        for row in rows
        if row.status == "ready"
    )

    return {
        "documents_total": len(rows),
        "documents_ready": statuses["ready"],
        "documents_processing": statuses["processing"],
        "documents_failed": statuses["failed"],
        "chunks_indexed": ready_chunks,
        "stages": [
            {
                "name": "Upload",
                "status": "complete" if rows else "waiting",
                "detail": f"{len(rows)} document(s) recorded",
            },
            {
                "name": "Text extraction",
                "status": (
                    "complete" if statuses["ready"]
                    else "error" if statuses["failed"]
                    else "waiting"
                ),
                "detail": (
                    f"{statuses['ready']} ready; "
                    f"{statuses['failed']} failed"
                ),
            },
            {
                "name": "Chunking",
                "status": "complete" if total_chunks else "waiting",
                "detail": f"{total_chunks} chunks recorded",
            },
            {
                "name": "Embeddings + vector index",
                "status": "complete" if statuses["ready"] else "waiting",
                "detail": "Uses the current ChromaDB ingestion path",
            },
        ],
    }


# --------------------------------------------------
# Retrieval diagnostics
# --------------------------------------------------

@app.post("/api/retrieval/debug")
def retrieval_debug(
    body: ChatRequest,
    user: User = Depends(admin_user),
):
    query = body.question.strip()

    if not query:
        raise HTTPException(
            status_code=422,
            detail="question cannot be empty",
        )

    try:
        contexts = retrieve(
            query=query,
            owner_user_id=user.id,
            limit=5,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Retrieval diagnostics failed: {str(exc)[:180]}",
        ) from exc

    return {
        "query": query,
        "result_count": len(contexts),
        "results": [
            {
                "filename": item["metadata"].get(
                    "filename", "unknown"
                ),
                "document_id": item["metadata"].get("document_id"),
                "chunk_index": item["metadata"].get("chunk_index"),
                "distance": item["distance"],
                "similarity_estimate": (
                    round(
                        max(
                            0.0,
                            min(
                                1.0,
                                1.0 - float(item["distance"]),
                            ),
                        ),
                        4,
                    )
                    if item["distance"] is not None
                    else None
                ),
                "text_preview": item["text"][:600],
            }
            for item in contexts
        ],
    }
