from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status

from guardrail.config import SnowflakeSettings, TransportSettings
from guardrail.domain.events import SimulationRequest, SimulationResult
from guardrail.domain.experiments import Assignment, Experiment, ExperimentCreate
from guardrail.domain.ingestion import (
    EventBatch,
    EventPublisher,
    EventPublishingError,
)
from guardrail.domain.integrity import (
    ExperimentHealthReport,
    IntegrityEvidenceError,
    IntegrityEvidenceRepository,
)
from guardrail.domain.replay import (
    InvalidReplayPayloadError,
    RejectedEvent,
    RejectionNotFoundError,
    ReplayConflictError,
    ReplayExecutionError,
    ReplayReceipt,
    ReplayRequest,
    ReplayWorkflow,
)
from guardrail.domain.transport import (
    EventDispatcher,
    EventDispatchError,
    EventDispatchReceipt,
)
from guardrail.infrastructure.integrity import SnowflakeIntegrityRepository
from guardrail.infrastructure.redpanda import RedpandaEventDispatcher
from guardrail.infrastructure.replay import SnowflakeReplayRepository
from guardrail.infrastructure.streaming import SnowpipeEventPublisher
from guardrail.services.assignment import AssignmentService
from guardrail.services.integrity import IntegrityService
from guardrail.services.replay import ReplayService
from guardrail.services.simulator import DEFAULT_SIMULATION_START, MarketplaceSimulator
from guardrail.services.store import (
    ExperimentAlreadyExistsError,
    ExperimentNotFoundError,
    InMemoryExperimentStore,
    InvalidExperimentStateError,
)
from guardrail.services.transport import DirectSnowpipeDispatcher


def create_app(
    store: InMemoryExperimentStore | None = None,
    event_publisher: EventPublisher | None = None,
    replay_workflow: ReplayWorkflow | None = None,
    event_dispatcher: EventDispatcher | None = None,
    integrity_repository: IntegrityEvidenceRepository | None = None,
) -> FastAPI:
    experiment_store = store or InMemoryExperimentStore()
    assignment_service = AssignmentService(experiment_store)
    simulator = MarketplaceSimulator()
    publisher = event_publisher
    replay_service = replay_workflow
    dispatcher = event_dispatcher
    evidence_repository = integrity_repository
    integrity_service: IntegrityService | None = None
    snowflake_settings: SnowflakeSettings | None = None
    transport_settings: TransportSettings | None = None

    def get_snowflake_settings() -> SnowflakeSettings:
        nonlocal snowflake_settings
        if snowflake_settings is None:
            snowflake_settings = SnowflakeSettings()  # type: ignore[call-arg]
        return snowflake_settings

    def get_event_publisher() -> EventPublisher:
        nonlocal publisher
        if publisher is None:
            publisher = SnowpipeEventPublisher(get_snowflake_settings())
        return publisher

    def get_transport_settings() -> TransportSettings:
        nonlocal transport_settings
        if transport_settings is None:
            transport_settings = TransportSettings()  # type: ignore[call-arg]
        return transport_settings

    def get_event_dispatcher() -> EventDispatcher:
        nonlocal dispatcher
        if dispatcher is None:
            settings = get_transport_settings()
            if settings.guardrail_event_transport == "redpanda":
                dispatcher = RedpandaEventDispatcher(settings)
            else:
                dispatcher = DirectSnowpipeDispatcher(get_event_publisher())
        return dispatcher

    def get_replay_workflow() -> ReplayWorkflow:
        nonlocal replay_service
        if replay_service is None:
            replay_service = ReplayService(
                SnowflakeReplayRepository(get_snowflake_settings()),
                get_event_publisher(),
            )
        return replay_service

    def get_integrity_service() -> IntegrityService:
        nonlocal evidence_repository, integrity_service
        if integrity_service is None:
            if evidence_repository is None:
                evidence_repository = SnowflakeIntegrityRepository(get_snowflake_settings())
            integrity_service = IntegrityService(evidence_repository)
        return integrity_service

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if dispatcher is not None and hasattr(dispatcher, "close"):
            dispatcher.close()  # type: ignore[attr-defined]
        if publisher is not None and hasattr(publisher, "close"):
            publisher.close()  # type: ignore[attr-defined]

    application = FastAPI(
        title="Guardrail API",
        version="0.1.0",
        description="Experiment assignment and marketplace telemetry simulation.",
        lifespan=lifespan,
    )

    @application.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "guardrail-api"}

    @application.post(
        "/experiments",
        response_model=Experiment,
        status_code=status.HTTP_201_CREATED,
        tags=["experiments"],
    )
    def create_experiment(request: ExperimentCreate) -> Experiment:
        try:
            return experiment_store.create_experiment(request)
        except ExperimentAlreadyExistsError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Experiment '{request.experiment_id}' already exists.",
            ) from exc

    @application.get(
        "/experiments/{experiment_id}",
        response_model=Experiment,
        tags=["experiments"],
    )
    def get_experiment(experiment_id: str) -> Experiment:
        try:
            return experiment_store.get_experiment(experiment_id)
        except ExperimentNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Experiment '{experiment_id}' was not found.",
            ) from exc

    @application.get(
        "/experiments/{experiment_id}/health",
        response_model=ExperimentHealthReport,
        tags=["experiments"],
    )
    def get_experiment_health(experiment_id: str) -> ExperimentHealthReport:
        try:
            experiment = experiment_store.get_experiment(experiment_id)
            return get_integrity_service().get_health(
                experiment.experiment_id,
                experiment.allocation_version,
                experiment.treatment_percentage,
            )
        except ExperimentNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Experiment '{experiment_id}' was not found.",
            ) from exc
        except IntegrityEvidenceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Experiment integrity evidence is temporarily unavailable.",
            ) from exc

    @application.post(
        "/experiments/{experiment_id}/launch",
        response_model=Experiment,
        tags=["experiments"],
    )
    def launch_experiment(experiment_id: str) -> Experiment:
        try:
            return experiment_store.launch_experiment(experiment_id)
        except ExperimentNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Experiment '{experiment_id}' was not found.",
            ) from exc
        except InvalidExperimentStateError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

    @application.get(
        "/assignments/{experiment_id}/{subject_id}",
        response_model=Assignment,
        tags=["assignments"],
    )
    def get_assignment(experiment_id: str, subject_id: str) -> Assignment:
        try:
            return assignment_service.assign(experiment_id, subject_id)
        except ExperimentNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Experiment '{experiment_id}' was not found.",
            ) from exc
        except InvalidExperimentStateError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

    @application.post(
        "/events",
        response_model=EventDispatchReceipt,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["ingestion"],
    )
    def ingest_events(request: EventBatch) -> EventDispatchReceipt:
        try:
            return get_event_dispatcher().dispatch(request.events)
        except (EventDispatchError, EventPublishingError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Event transport is temporarily unavailable.",
            ) from exc

    @application.get(
        "/rejections/{rejection_id}",
        response_model=RejectedEvent,
        tags=["replay"],
    )
    def get_rejected_event(rejection_id: str) -> RejectedEvent:
        try:
            return get_replay_workflow().get_rejection(rejection_id)
        except RejectionNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rejected event '{rejection_id}' was not found.",
            ) from exc

    @application.post(
        "/rejections/{rejection_id}/replay",
        response_model=ReplayReceipt,
        tags=["replay"],
    )
    def replay_rejected_event(rejection_id: str, request: ReplayRequest) -> ReplayReceipt:
        try:
            return get_replay_workflow().replay(rejection_id, request)
        except RejectionNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rejected event '{rejection_id}' was not found.",
            ) from exc
        except ReplayConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except InvalidReplayPayloadError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ReplayExecutionError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Corrected event replay is temporarily unavailable.",
            ) from exc

    @application.post(
        "/simulations",
        response_model=SimulationResult,
        tags=["simulation"],
    )
    def run_simulation(request: SimulationRequest) -> SimulationResult:
        assignments: list[Assignment] = []
        events = []
        try:
            for index in range(request.subjects):
                assignment = assignment_service.assign(request.experiment_id, f"user_{index:06d}")
                assignments.append(assignment)
                events.extend(
                    simulator.generate_journey(
                        assignment,
                        seed=request.seed,
                        started_at=DEFAULT_SIMULATION_START,
                        fault_profile=request.fault_profile,
                    )
                )
        except ExperimentNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Experiment '{request.experiment_id}' was not found.",
            ) from exc
        except InvalidExperimentStateError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

        result = simulator.summarize(request.experiment_id, assignments, events)
        if request.persist_events:
            try:
                dispatch = get_event_dispatcher().dispatch(events)
            except (EventDispatchError, EventPublishingError, ValueError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Simulation completed, but event ingestion failed.",
                ) from exc
            result = result.model_copy(
                update={
                    "telemetry_batch_id": dispatch.batch_id,
                    "telemetry_transport": dispatch.transport.value,
                    "ingestion_batch_id": (
                        dispatch.ingestion.batch_id if dispatch.ingestion is not None else None
                    ),
                }
            )

        return result

    return application


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("guardrail.main:app", host="0.0.0.0", port=8000, reload=True)
