import { useCallback, useEffect, useState } from "react";
import api from "../api/client";
import type { StageExecutionDetail, StageExecutionSummary } from "../types";

export function useStageExecutions(
  projectId: number | null,
  stageNumber: number | null,
  workflowId: string | null | undefined,
  refreshKey: unknown,
) {
  const [executions, setExecutions] = useState<StageExecutionSummary[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!projectId || stageNumber == null) {
      setExecutions([]);
      return;
    }
    setLoading(true);
    try {
      const params: Record<string, string> = {};
      if (workflowId) params.workflow_id = workflowId;
      const res = await api.get<StageExecutionSummary[]>(
        `/api/projects/${projectId}/stages/${stageNumber}/executions`,
        { params },
      );
      setExecutions(res.data);
    } catch {
      setExecutions([]);
    } finally {
      setLoading(false);
    }
  }, [projectId, stageNumber, workflowId]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  return { executions, loading, reload: load };
}

export function useStageExecution(
  executionId: number | null,
  projectId: number | null = null,
) {
  const [detail, setDetail] = useState<StageExecutionDetail | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (executionId == null) {
      setDetail(null);
      return;
    }
    setLoading(true);
    const params: Record<string, number> = {};
    if (projectId != null) params.project_id = projectId;
    api
      .get<StageExecutionDetail>(`/api/stage-executions/${executionId}`, { params })
      .then((res) => {
        if (!cancelled) setDetail(res.data);
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [executionId, projectId]);

  return { detail, loading };
}
