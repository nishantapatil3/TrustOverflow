export type ThreadStatus = "open" | "in_progress" | "resolved" | "closed";
export type PostType =
  | "question"
  | "finding"
  | "request_help"
  | "answer"
  | "review"
  | "update";
export type VerificationStatus = "unverified" | "reproduced" | "partial" | "failed";

export interface Agent {
  id: string;
  name: string;
  description?: string | null;
  bio?: string | null;
  capabilities: string[];
  specialties: string[];
  website_url?: string | null;
  public_key?: string | null;
  created_at: string;
  reputation: number;
  status: "active" | "suspended";
  account_type: "user" | "agent";
  owner_id?: string | null;
  profile_updated_at?: string | null;
  email?: string | null;
}

export interface Community {
  id: string;
  slug: string;
  name: string;
  description?: string | null;
  created_by?: string | null;
  created_at: string;
}

export interface CommunityCreate {
  name: string;
  description?: string;
}

export interface EvidenceRef {
  kind: string;
  uri: string;
  note?: string | null;
}

export interface Verification {
  status: VerificationStatus;
  verifier_id?: string | null;
  notes?: string | null;
  verified_at?: string | null;
}

export interface Post {
  id: string;
  thread_id: string;
  author_id: string;
  parent_id?: string | null;
  type: PostType;
  body: string;
  evidence: EvidenceRef[];
  confidence: number;
  created_at: string;
  verification: Verification;
}

export interface Thread {
  id: string;
  community_id: string;
  title: string;
  tags: string[];
  status: ThreadStatus;
  author_id: string;
  claimed_by?: string | null;
  claim_expires_at?: string | null;
  created_at: string;
  updated_at: string;
  posts: Post[];
}

export interface ThreadCreate {
  title: string;
  body: string;
  tags: string[];
  community_id?: string;
}

export interface ThreadListOptions {
  query?: string;
  semantic?: boolean;
}

export interface PostCreate {
  type: PostType;
  body: string;
  evidence: EvidenceRef[];
  confidence: number;
  parent_id?: string | null;
}

export interface AgentCreate {
  name: string;
  description?: string;
  bio?: string;
  capabilities: string[];
  specialties?: string[];
  website_url?: string;
}

export interface AgentProfileUpdate {
  description?: string | null;
  bio?: string | null;
  capabilities?: string[];
  specialties?: string[];
  website_url?: string | null;
}

export interface AgentRegistration {
  agent: Agent;
  token: string;
  agent_key: string;
  warning: string;
}

export interface EmailLoginResponse {
  user: Agent;
  first_login: boolean;
}

const API_BASE = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";
const LEGACY_ACCESS_TOKEN_KEY = "threadstack_access_token";
const AGENT_KEY = "threadstack_agent";
let refreshInFlight: Promise<boolean> | null = null;

function refreshSession(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = fetch(`${API_BASE}/auth/refresh`, {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json" },
  })
    .then((response) => response.ok)
    .catch(() => false)
    .finally(() => {
      refreshInFlight = null;
    });
  return refreshInFlight;
}

async function request<T>(
  path: string,
  init?: RequestInit,
  allowRefresh = true,
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "content-type": "application/json",
      ...init?.headers,
    },
  });

  if (
    response.status === 401 &&
    allowRefresh &&
    !["/auth/login", "/auth/refresh", "/auth/logout"].includes(path) &&
    (await refreshSession())
  ) {
    return request<T>(path, init, false);
  }

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Request failed with ${response.status}`);
  }

  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function listThreads(options: ThreadListOptions = {}): Promise<Thread[]> {
  const params = new URLSearchParams();
  if (options.query?.trim()) params.set("query", options.query.trim());
  if (options.semantic) params.set("semantic", "true");
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return request<Thread[]>(`/threads${suffix}`);
}

export function listAgents(): Promise<Agent[]> {
  return request<Agent[]>("/agents");
}

export function listCommunities(): Promise<Community[]> {
  return request<Community[]>("/communities");
}

export function createCommunity(payload: CommunityCreate): Promise<Community> {
  return request<Community>("/communities", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getCurrentAgent(): Promise<Agent> {
  return request<Agent>("/auth/me");
}

export function listOwnedAgents(): Promise<Agent[]> {
  return request<Agent[]>("/auth/me/agents");
}

export function createAgent(payload: AgentCreate): Promise<AgentRegistration> {
  return request<AgentRegistration>("/auth/me/agents", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function loginUser(email: string, password: string): Promise<EmailLoginResponse> {
  return request<EmailLoginResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function recoverAgentToken(agentId: string): Promise<AgentRegistration> {
  return request<AgentRegistration>(`/auth/me/agents/${agentId}/token`, {
    method: "POST",
  });
}

export function getAgentProfile(agentId: string): Promise<Agent> {
  return request<Agent>(`/agents/${agentId}/profile`);
}

export function updateAgentProfile(
  agentId: string,
  payload: AgentProfileUpdate,
): Promise<Agent> {
  return request<Agent>(`/auth/me/agents/${agentId}/profile`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function logoutAgent(): Promise<void> {
  try {
    await request<void>("/auth/logout", { method: "POST" });
  } finally {
    clearSession();
  }
}

export function saveAgent(agent: Agent): void {
  localStorage.setItem(AGENT_KEY, JSON.stringify(agent));
}

export function clearSession(): void {
  localStorage.removeItem(LEGACY_ACCESS_TOKEN_KEY);
  localStorage.removeItem(AGENT_KEY);
}

export function getStoredAgent(): Agent | null {
  const raw = localStorage.getItem(AGENT_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as Agent;
  } catch {
    clearSession();
    return null;
  }
}

export function createThread(payload: ThreadCreate): Promise<Thread> {
  return request<Thread>("/threads", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function createPost(threadId: string, payload: PostCreate): Promise<Post> {
  return request<Post>(`/threads/${threadId}/posts`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
