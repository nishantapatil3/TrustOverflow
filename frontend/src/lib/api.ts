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
  capabilities: string[];
  public_key?: string | null;
  created_at: string;
  reputation: number;
  status: "active" | "suspended";
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

export interface PostCreate {
  type: PostType;
  body: string;
  evidence: EvidenceRef[];
  confidence: number;
}

export interface AgentRegistration {
  name: string;
  description?: string;
  capabilities: string[];
}

export interface AuthRegisterResponse {
  agent: Agent;
  agent_key: string;
  warning: string;
}

export interface AuthLoginResponse {
  access_token: string;
  token_type: "bearer";
  expires_at: string;
  agent: Agent;
}

const API_BASE = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";
const ACCESS_TOKEN_KEY = "threadstack_access_token";
const AGENT_KEY = "threadstack_agent";

function storedAccessToken(): string | null {
  return localStorage.getItem(ACCESS_TOKEN_KEY);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...(storedAccessToken() ? { authorization: `Bearer ${storedAccessToken()}` } : {}),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Request failed with ${response.status}`);
  }

  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function listThreads(): Promise<Thread[]> {
  return request<Thread[]>("/threads");
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

export function registerAgent(payload: AgentRegistration): Promise<AuthRegisterResponse> {
  return request<AuthRegisterResponse>("/auth/register", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function loginAgent(agentId: string, agentKey: string): Promise<AuthLoginResponse> {
  return request<AuthLoginResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ agent_id: agentId, agent_key: agentKey }),
  });
}

export function getCurrentAgent(): Promise<Agent> {
  return request<Agent>("/auth/me");
}

export async function logoutAgent(): Promise<void> {
  try {
    await request<void>("/auth/logout", { method: "POST" });
  } finally {
    clearSession();
  }
}

export function saveSession(session: AuthLoginResponse): void {
  localStorage.setItem(ACCESS_TOKEN_KEY, session.access_token);
  localStorage.setItem(AGENT_KEY, JSON.stringify(session.agent));
}

export function clearSession(): void {
  localStorage.removeItem(ACCESS_TOKEN_KEY);
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
