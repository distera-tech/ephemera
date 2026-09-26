# ADR-003: A dedicated worker backed by PostgreSQL, not a message broker

**Status:** accepted

**Context.** Jobs take minutes, run one or two at a time, and must be recoverable after
crashes. Celery/RabbitMQ/Redis/Kafka/Temporal would add operational surface to a
hackathon MVP.

**Decision.** A single-purpose Python worker polls PostgreSQL (`FOR UPDATE SKIP LOCKED`
under an advisory lock), heart-beats the job it runs, and reconciles on start-up and
periodically. The database *is* the durable state machine.

**Consequences.** One fewer service; recovery logic lives next to the state it
recovers. Throughput is limited to polling cadence — irrelevant when GPU provisioning
takes minutes. If scale ever demands it, the claim loop can be replaced without touching
the orchestrator.
