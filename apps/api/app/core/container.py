"""Composition root: wires ports to adapters based on configuration."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.application.orchestrator import OrchestratorConfig
from app.core.config import InferenceEngine, Settings
from app.domain.ports import ComputeProvider, InferenceProvider
from app.infrastructure.brev.client import BrevClient
from app.infrastructure.brev.lifecycle import BrevComputeProvider
from app.infrastructure.compute.simulated import SimulatedComputeProvider
from app.infrastructure.database.repository import PostgresJobRepository
from app.infrastructure.database.session import create_engine, create_sessionmaker
from app.infrastructure.documents.pymupdf_processor import PyMuPDFDocumentProcessor
from app.infrastructure.inference.nim import build_nim_provider
from app.infrastructure.inference.simulated import SIMULATED_MODEL_ID, SimulatedInferenceProvider
from app.infrastructure.inference.vllm import VLLMProvider
from app.infrastructure.storage.workspace import JobWorkspace


def effective_model_id(settings: Settings) -> str:
    return SIMULATED_MODEL_ID if settings.is_simulation else settings.model_id


def build_compute(settings: Settings) -> ComputeProvider:
    if settings.is_simulation:
        return SimulatedComputeProvider(time_scale=settings.simulation_time_scale)
    client = BrevClient(
        cli_path=settings.brev_cli_path,
        home=settings.brev_home,
        api_key=settings.brev_api_key,
        org=settings.brev_org,
        exec_on_host=settings.brev_exec_on_host,
        ssh_connect_timeout_s=settings.brev_ssh_connect_timeout_seconds,
    )
    return BrevComputeProvider(
        client,
        ssh_unreachable_timeout_s=settings.brev_ssh_unreachable_timeout_seconds,
        excluded_providers=tuple(settings.brev_excluded_providers.split(",")),
    )


def build_inference(settings: Settings) -> InferenceProvider:
    if settings.is_simulation:
        return SimulatedInferenceProvider(time_scale=settings.simulation_time_scale)
    if settings.inference_engine is InferenceEngine.NIM:
        build_nim_provider()  # raises ConfigurationError: not implemented yet
    return VLLMProvider(
        model_id=settings.model_id,
        image=settings.vllm_image,
        max_model_len=settings.vllm_max_model_len,
        gpu_memory_utilization=settings.vllm_gpu_memory_utilization,
        hf_token=settings.hf_token.get_secret_value() if settings.hf_token else None,
        transfer_timeout_s=settings.transfer_timeout_seconds,
        inference_timeout_s=settings.inference_timeout_seconds,
        min_free_disk_gb=settings.gpu_min_free_disk_gb,
    )


def build_documents(settings: Settings) -> PyMuPDFDocumentProcessor:
    return PyMuPDFDocumentProcessor(
        max_pages=settings.max_document_pages,
        max_chars=settings.max_input_chars,
        timeout_s=settings.extraction_timeout_seconds,
    )


def orchestrator_config(settings: Settings) -> OrchestratorConfig:
    return OrchestratorConfig(
        model_id=effective_model_id(settings),
        gpu_requirements=settings.gpu_requirements(),
        remote_data_dir=settings.remote_data_dir,
        max_job_runtime_s=settings.max_job_runtime_seconds,
        max_provisioning_s=settings.max_provisioning_seconds,
        bootstrap_timeout_s=settings.bootstrap_timeout_seconds,
        model_ready_timeout_s=settings.model_ready_timeout_seconds,
        transfer_timeout_s=settings.transfer_timeout_seconds,
        inference_timeout_s=settings.inference_timeout_seconds,
        cleanup_timeout_s=settings.cleanup_timeout_seconds,
        destroy_timeout_s=settings.destroy_timeout_seconds,
        destroy_verify_timeout_s=settings.destroy_verify_timeout_seconds,
        max_output_tokens=settings.max_output_tokens,
        result_retention_s=settings.result_retention_seconds,
        heartbeat_interval_s=settings.heartbeat_interval_seconds,
        force_inference_failure=settings.demo_force_inference_failure,
    )


@dataclass(slots=True)
class Database:
    engine: AsyncEngine
    repo: PostgresJobRepository


def build_database(settings: Settings) -> Database:
    engine = create_engine(settings.database_url)
    return Database(engine=engine, repo=PostgresJobRepository(create_sessionmaker(engine)))


def build_workspace(settings: Settings) -> JobWorkspace:
    return JobWorkspace(settings.data_dir)
