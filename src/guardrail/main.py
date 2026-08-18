from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from guardrail.config import RuntimeSettings, SnowflakeSettings, TransportSettings
from guardrail.domain.analysis import (
    AnalysisEvidenceError,
    AnalysisEvidenceRepository,
    AnalysisSnapshotError,
    AnalysisSnapshotRepository,
    ExperimentAnalysisReport,
)
from guardrail.domain.auth import ApplicationRole, Principal, TokenAuthenticator
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
from guardrail.domain.metrics import MetricArtifactPersistenceError
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
from guardrail.domain.rollout import (
    DecisionEvidenceError,
    DecisionPersistenceError,
    InvalidRolloutTransitionError,
    RolloutDecisionRecord,
    RolloutPolicyError,
    RolloutTransitionOutcome,
    RolloutWorkflow,
    TransitionRequest,
)
from guardrail.domain.transport import (
    EventDispatcher,
    EventDispatchError,
    EventDispatchReceipt,
)
from guardrail.infrastructure.analysis import SnowflakeAnalysisRepository
from guardrail.infrastructure.http_security import RequestSecurityMiddleware
from guardrail.infrastructure.integrity import SnowflakeIntegrityRepository
from guardrail.infrastructure.metrics import SnowflakeMetricArtifactRepository
from guardrail.infrastructure.postgres import (
    PostgresExperimentStore,
    PostgresRolloutRepository,
)
from guardrail.infrastructure.redpanda import RedpandaEventDispatcher
from guardrail.infrastructure.replay import SnowflakeReplayRepository
from guardrail.infrastructure.rollout import (
    SnowflakeDecisionEvidenceRepository,
    SnowflakeDecisionRepository,
)
from guardrail.infrastructure.snapshots import SnowflakeAnalysisSnapshotRepository
from guardrail.infrastructure.streaming import SnowpipeEventPublisher
from guardrail.infrastructure.telemetry import (
    HttpMetricsMiddleware,
    configure_json_logging,
    prometheus_response,
)
from guardrail.services.analysis import ExperimentAnalysisService
from guardrail.services.assignment import AssignmentService
from guardrail.services.integrity import IntegrityService
from guardrail.services.replay import ReplayService
from guardrail.services.rollout import RolloutPolicy, RolloutPolicyEvaluator, RolloutService
from guardrail.services.security import AuthorizationService
from guardrail.services.simulator import DEFAULT_SIMULATION_START, MarketplaceSimulator
from guardrail.services.store import (
    ConcurrentExperimentUpdateError,
    ExperimentAlreadyExistsError,
    ExperimentNotFoundError,
    ExperimentStore,
    InMemoryExperimentStore,
    InvalidExperimentStateError,
)
from guardrail.services.transport import DirectSnowpipeDispatcher


def create_app(
    store: ExperimentStore | None = None,
    event_publisher: EventPublisher | None = None,
    replay_workflow: ReplayWorkflow | None = None,
    event_dispatcher: EventDispatcher | None = None,
    integrity_repository: IntegrityEvidenceRepository | None = None,
    analysis_repository: AnalysisEvidenceRepository | None = None,
    analysis_snapshot_repository: AnalysisSnapshotRepository | None = None,
    rollout_workflow: RolloutWorkflow | None = None,
    runtime_settings: RuntimeSettings | None = None,
    token_authenticator: TokenAuthenticator | None = None,
) -> FastAPI:
    runtime = runtime_settings or RuntimeSettings()
    use_default_rollout_workflow = rollout_workflow is None
    experiment_store = store or (
        PostgresExperimentStore(runtime)
        if runtime.guardrail_store_backend == "postgres"
        else InMemoryExperimentStore()
    )
    operational_rollout_repository = (
        PostgresRolloutRepository(experiment_store.engine)
        if isinstance(experiment_store, PostgresExperimentStore)
        else None
    )
    assignment_service = AssignmentService(experiment_store)
    authorization = AuthorizationService(runtime, token_authenticator)
    simulator = MarketplaceSimulator()
    publisher = event_publisher
    replay_service = replay_workflow
    dispatcher = event_dispatcher
    evidence_repository = integrity_repository
    use_default_snapshot_repository = analysis_repository is None
    metric_evidence_repository = analysis_repository
    snapshot_repository = analysis_snapshot_repository
    rollout_service = rollout_workflow
    integrity_service: IntegrityService | None = None
    analysis_service: ExperimentAnalysisService | None = None
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

    def get_analysis_service() -> ExperimentAnalysisService:
        nonlocal analysis_service, metric_evidence_repository, snapshot_repository
        if analysis_service is None:
            if metric_evidence_repository is None:
                metric_evidence_repository = SnowflakeAnalysisRepository(get_snowflake_settings())
            if snapshot_repository is None and use_default_snapshot_repository:
                snapshot_repository = SnowflakeAnalysisSnapshotRepository(get_snowflake_settings())
            analysis_service = ExperimentAnalysisService(
                get_integrity_service(),
                metric_evidence_repository,
                snapshot_repository=snapshot_repository,
            )
        return analysis_service

    def get_rollout_workflow() -> RolloutWorkflow:
        nonlocal rollout_service
        if rollout_service is None:
            settings = get_snowflake_settings()
            active_metrics = SnowflakeMetricArtifactRepository(settings).list_active()
            decision_repository = (
                operational_rollout_repository
                if operational_rollout_repository is not None
                else SnowflakeDecisionRepository(settings)
            )
            rollout_service = RolloutService(
                experiment_store,
                get_analysis_service(),
                SnowflakeDecisionEvidenceRepository(settings),
                decision_repository,
                atomic_repository=operational_rollout_repository,
                evaluator=RolloutPolicyEvaluator(
                    RolloutPolicy.from_metric_definitions(
                        artifact.definition for artifact in active_metrics
                    )
                ),
            )
        return rollout_service

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if dispatcher is not None and hasattr(dispatcher, "close"):
            dispatcher.close()  # type: ignore[attr-defined]
        if publisher is not None and hasattr(publisher, "close"):
            publisher.close()  # type: ignore[attr-defined]
        if hasattr(experiment_store, "close"):
            experiment_store.close()  # type: ignore[attr-defined]

    application = FastAPI(
        title="Guardrail API",
        version="0.2.0",
        description="Experiment assignment and marketplace telemetry simulation.",
        lifespan=lifespan,
        docs_url=None if runtime.guardrail_env == "production" else "/docs",
        redoc_url=None if runtime.guardrail_env == "production" else "/redoc",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime.cors_origin_values),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(runtime.trusted_host_values),
    )
    application.add_middleware(
        RequestSecurityMiddleware,
        max_request_bytes=runtime.max_request_bytes,
        production=runtime.guardrail_env == "production",
    )
    application.add_middleware(HttpMetricsMiddleware)

    viewer = authorization.require(ApplicationRole.VIEWER)
    operator = authorization.require(ApplicationRole.EXPERIMENT_OPERATOR)
    approver = authorization.require(ApplicationRole.ROLLOUT_APPROVER)
    assignment_client = authorization.require(ApplicationRole.ASSIGNMENT_CLIENT)
    telemetry_producer = authorization.require(ApplicationRole.TELEMETRY_PRODUCER)

    @application.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "guardrail-api"}

    @application.get("/health/live", tags=["system"])
    def liveness() -> dict[str, str]:
        return {"status": "ok", "service": "guardrail-api"}

    @application.get("/health/ready", tags=["system"])
    def readiness() -> dict[str, str]:
        try:
            experiment_store.ping()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Operational state store is unavailable.",
            ) from exc
        return {"status": "ready", "store": runtime.guardrail_store_backend}

    @application.get("/metrics", include_in_schema=False)
    def metrics() -> object:
        return prometheus_response()

    @application.get("/me", response_model=Principal, tags=["system"])
    def current_principal(
        principal: Principal = Depends(authorization.any_authenticated),  # noqa: B008
    ) -> Principal:
        return principal

    @application.post(
        "/experiments",
        response_model=Experiment,
        status_code=status.HTTP_201_CREATED,
        tags=["experiments"],
        dependencies=[Depends(operator)],
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
        dependencies=[Depends(viewer)],
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
        dependencies=[Depends(viewer)],
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

    @application.get(
        "/experiments/{experiment_id}/results",
        response_model=ExperimentAnalysisReport,
        tags=["experiments"],
        dependencies=[Depends(viewer)],
    )
    def get_experiment_results(experiment_id: str) -> ExperimentAnalysisReport:
        try:
            experiment = experiment_store.get_experiment(experiment_id)
            return get_analysis_service().get_results(
                experiment.experiment_id,
                experiment.allocation_version,
                experiment.treatment_percentage,
            )
        except ExperimentNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Experiment '{experiment_id}' was not found.",
            ) from exc
        except (IntegrityEvidenceError, AnalysisEvidenceError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Experiment analysis evidence is temporarily unavailable.",
            ) from exc
        except AnalysisSnapshotError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Experiment analysis snapshot could not be persisted.",
            ) from exc

    @application.get(
        "/experiments/{experiment_id}/decisions",
        response_model=list[RolloutDecisionRecord],
        tags=["experiments"],
        dependencies=[Depends(viewer)],
    )
    def get_experiment_decisions(experiment_id: str) -> tuple[RolloutDecisionRecord, ...]:
        try:
            if operational_rollout_repository is not None and use_default_rollout_workflow:
                experiment_store.get_experiment(experiment_id)
                return operational_rollout_repository.list_decisions(experiment_id)
            return get_rollout_workflow().list_decisions(experiment_id)
        except ExperimentNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Experiment '{experiment_id}' was not found.",
            ) from exc
        except (
            DecisionPersistenceError,
            MetricArtifactPersistenceError,
            RolloutPolicyError,
        ) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rollout decision history is temporarily unavailable.",
            ) from exc

    @application.post(
        "/experiments/{experiment_id}/transitions",
        response_model=RolloutTransitionOutcome,
        tags=["experiments"],
    )
    def apply_experiment_transition(
        experiment_id: str,
        request: TransitionRequest,
        principal: Principal = Depends(approver),  # noqa: B008
    ) -> RolloutTransitionOutcome:
        if runtime.auth_enabled and request.approved_by != principal.subject:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="approved_by must match the authenticated rollout approver.",
            )
        try:
            return get_rollout_workflow().evaluate_and_transition(experiment_id, request)
        except ExperimentNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Experiment '{experiment_id}' was not found.",
            ) from exc
        except InvalidRolloutTransitionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except ConcurrentExperimentUpdateError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Experiment changed during rollout evaluation; retry with fresh evidence.",
            ) from exc
        except (
            IntegrityEvidenceError,
            AnalysisEvidenceError,
            AnalysisSnapshotError,
            DecisionEvidenceError,
            DecisionPersistenceError,
            MetricArtifactPersistenceError,
            RolloutPolicyError,
        ) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rollout decision could not be evaluated or persisted.",
            ) from exc

    @application.post(
        "/experiments/{experiment_id}/launch",
        response_model=Experiment,
        tags=["experiments"],
        dependencies=[Depends(operator)],
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
        dependencies=[Depends(assignment_client)],
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
        dependencies=[Depends(telemetry_producer)],
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
        dependencies=[Depends(viewer)],
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
        dependencies=[Depends(operator)],
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
        dependencies=[Depends(operator)],
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

    runtime = RuntimeSettings()
    configure_json_logging()
    uvicorn.run(
        "guardrail.main:app",
        host="0.0.0.0",
        port=8000,
        reload=runtime.guardrail_env == "local",
        proxy_headers=True,
        forwarded_allow_ips="*" if runtime.guardrail_env == "local" else "127.0.0.1",
        log_config=None,
    )
