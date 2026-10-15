import { create } from "zustand";
import {
  beginLogtoLogin,
  clearToken,
  completeLogtoCallback,
  getRolesFromToken,
  getToken,
  isLogtoConfigured,
  isLogtoPartiallyConfigured,
  isValidAccessToken,
  logtoLogoutUrl,
  setToken as saveToken,
} from "@/utils/auth";

interface AuthState {
  token: string | null;
  username: string | null;
  roles: string[];
  isAuthenticated: boolean;
  isLoading: boolean;
  initialize: () => void;
  login: (token: string, username: string, roles?: string[]) => void;
  loginWithLogto: (nextPath?: string) => Promise<void>;
  completeLogtoCallback: () => Promise<boolean>;
  isAdmin: () => boolean;
  logout: () => void;
  setLoading: (loading: boolean) => void;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  token: null,
  username: null,
  roles: [],
  isAuthenticated: false,
  isLoading: true,

  initialize: () => {
    const token = getToken();
    if (token && (!isLogtoConfigured() || isValidAccessToken(token))) {
      set({
        token,
        roles: isLogtoConfigured() ? getRolesFromToken(token) : ["administrator"],
        isAuthenticated: true,
        isLoading: false,
      });
      return;
    }
    if (token) clearToken();
    if (isLogtoPartiallyConfigured()) {
      console.warn("[auth] Studio360 Logto configuration is incomplete; staying signed out.");
      set({ isLoading: false });
      return;
    }
    if (isLogtoConfigured()) {
      set({ isLoading: false });
      return;
    }
    // 无 token 时先问后端是否启用了鉴权。`AUTH_ENABLED=false` 时后端全链路
    // bypass，前端也应该跳过登录页直接进主界面。超时 / 网络异常 / 响应 shape
    // 异常时 fail-closed 退回到登录页，避免误把损坏响应当成"无需鉴权"放行。
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 5000);
    fetch("/api/v1/auth/status", { signal: controller.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(`status ${res.status}`);
        const payload: unknown = await res.json();
        if (
          typeof payload !== "object" ||
          payload === null ||
          typeof (payload as { enabled?: unknown }).enabled !== "boolean"
        ) {
          throw new Error("invalid /auth/status payload");
        }
        const { enabled } = payload as { enabled: boolean };
        if (!enabled) {
          set({ isAuthenticated: true });
        }
      })
      .catch((err) => {
        console.warn("[auth] /auth/status fetch failed; defaulting to login", err);
      })
      .finally(() => {
        clearTimeout(timeoutId);
        set({ isLoading: false });
      });
  },

  login: (token, username, roles = getRolesFromToken(token)) => {
    saveToken(token);
    set({ token, username, roles, isAuthenticated: true, isLoading: false });
  },

  loginWithLogto: async (nextPath = "/app/projects") => {
    await beginLogtoLogin(nextPath);
  },

  completeLogtoCallback: async () => {
    const result = await completeLogtoCallback();
    if (!result) return false;
    const token = getToken();
    if (!token) return false;
    set({
      token,
      username: result.username,
      roles: result.roles,
      isAuthenticated: true,
      isLoading: false,
    });
    return true;
  },

  isAdmin: (): boolean => {
    const roles = get().roles;
    return roles.some((role: string) => ["admin", "administrator", "owner"].includes(role.toLowerCase()));
  },

  logout: () => {
    clearToken();
    set({ token: null, username: null, roles: [], isAuthenticated: false });
    const url = logtoLogoutUrl();
    if (url) window.location.assign(url);
  },

  setLoading: (isLoading) => set({ isLoading }),
}));
