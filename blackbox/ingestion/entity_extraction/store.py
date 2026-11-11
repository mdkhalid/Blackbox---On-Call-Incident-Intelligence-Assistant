"""Entity store: structured facts (services, error codes, dependencies, doc entities, ingest tracking)."""
from __future__ import annotations

from typing import Protocol

from sqlalchemy import Column, String, Text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from blackbox.db import build_engine


class Base(DeclarativeBase):
    pass


class Service(Base):
    __tablename__ = "services"
    name = Column(String, primary_key=True)
    description = Column(Text, nullable=True)


class ErrorCode(Base):
    __tablename__ = "error_codes"
    code = Column(String, primary_key=True)
    description = Column(Text, nullable=True)


class Dependency(Base):
    __tablename__ = "dependencies"
    id = Column(String, primary_key=True)
    src = Column(String, nullable=False)
    dst = Column(String, nullable=False)
    dep_type = Column(String, default="depends_on")


class DocEntity(Base):
    __tablename__ = "doc_entities"
    id = Column(String, primary_key=True)
    doc_id = Column(String, nullable=False)
    entity_type = Column(String, nullable=False)
    entity_value = Column(String, nullable=False)


class IngestedDoc(Base):
    __tablename__ = "ingested_docs"
    doc_id = Column(String, primary_key=True)
    content_hash = Column(String, nullable=False)
    chunk_ids = Column(Text, nullable=True)  # comma-joined


class EntityStore(Protocol):
    def init(self) -> None: ...
    def upsert_service(self, name: str, description: str | None = None) -> None: ...
    def upsert_error_code(self, code: str, description: str | None = None) -> None: ...
    def upsert_dependency(self, src: str, dst: str, dep_type: str = "depends_on") -> None: ...
    def add_doc_entity(self, doc_id: str, entity_type: str, entity_value: str) -> None: ...
    def services_depending_on(self, service: str) -> list[str]: ...
    def get_services(self) -> list[str]: ...
    def get_error_codes(self) -> list[str]: ...
    def record_ingested(self, doc_id: str, content_hash: str, chunk_ids: list[str]) -> None: ...
    def get_ingested(self, doc_id: str) -> IngestedDoc | None: ...
    def delete_doc_entities(self, doc_id: str) -> None: ...
    def all_dependencies(self) -> list[tuple[str, str]]: ...


class SqlAlchemyEntityStore:
    def __init__(self, db_url: str = "sqlite:///:memory:"):
        self.engine = build_engine(db_url)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def init(self) -> None:
        Base.metadata.create_all(self.engine)

    def _session(self) -> Session:
        return self.SessionLocal()

    def upsert_service(self, name, description=None):
        with self._session() as s:
            obj = s.get(Service, name)
            if obj is None:
                obj = Service(name=name, description=description)
                s.add(obj)
            else:
                if description is not None:
                    obj.description = description
            s.commit()

    def upsert_error_code(self, code, description=None):
        with self._session() as s:
            obj = s.get(ErrorCode, code)
            if obj is None:
                obj = ErrorCode(code=code, description=description)
                s.add(obj)
            else:
                if description is not None:
                    obj.description = description
            s.commit()

    def upsert_dependency(self, src, dst, dep_type="depends_on"):

        did = f"{src}->{dst}:{dep_type}"
        with self._session() as s:
            obj = s.get(Dependency, did)
            if obj is None:
                obj = Dependency(id=did, src=src, dst=dst, dep_type=dep_type)
                s.add(obj)
            s.commit()

    def add_doc_entity(self, doc_id, entity_type, entity_value):
        import uuid

        eid = str(uuid.uuid4())
        with self._session() as s:
            s.add(DocEntity(id=eid, doc_id=doc_id, entity_type=entity_type, entity_value=entity_value))
            s.commit()

    def services_depending_on(self, service):
        with self._session() as s:
            rows = s.query(Dependency).filter(Dependency.dst == service).all()
            return sorted({r.src for r in rows})

    def get_services(self):
        with self._session() as s:
            return sorted(r.name for r in s.query(Service).all())

    def get_error_codes(self):
        with self._session() as s:
            return sorted(r.code for r in s.query(ErrorCode).all())

    def record_ingested(self, doc_id, content_hash, chunk_ids):
        with self._session() as s:
            obj = s.get(IngestedDoc, doc_id)
            if obj is None:
                obj = IngestedDoc(doc_id=doc_id, content_hash=content_hash, chunk_ids=",".join(chunk_ids))
                s.add(obj)
            else:
                obj.content_hash = content_hash
                obj.chunk_ids = ",".join(chunk_ids)
            s.commit()

    def get_ingested(self, doc_id):
        with self._session() as s:
            obj = s.get(IngestedDoc, doc_id)
            if obj is None:
                return None
            # detach copy
            return IngestedDoc(doc_id=obj.doc_id, content_hash=obj.content_hash, chunk_ids=obj.chunk_ids)

    def delete_doc_entities(self, doc_id):
        with self._session() as s:
            s.query(DocEntity).filter(DocEntity.doc_id == doc_id).delete()
            s.commit()

    def all_dependencies(self):
        with self._session() as s:
            return [(r.src, r.dst) for r in s.query(Dependency).all()]
