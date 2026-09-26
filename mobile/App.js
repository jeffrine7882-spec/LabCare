/*
 * LabSynch mobile — native alerts app.
 *
 * Purpose: receive LabSynch bell notifications as OS-level push alerts that
 * RING on this phone even when the browser is closed, the app is backgrounded,
 * or the phone is locked.
 *
 * Flow:
 *   1. Sign in with a LabSynch account (same credentials as the web app).
 *   2. The app asks for notification permission, gets a Firebase/expo push
 *      token, and POSTs it to /api/app/register.
 *   3. From then on, the LabSynch backend sends every bell notification to this
 *      device via Firebase Cloud Messaging → the OS rings it.
 *   4. The Alerts toggle mirrors the per-user sound choice; turning it off
 *      unregisters the device (no more phone alerts).
 *
 * `npx expo start` → scan the QR with Expo Go, or run a dev build.
 */
import React, { useEffect, useRef, useState } from "react";
import {
  ActivityIndicator,
  Platform,
  SafeAreaView,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from "react-native";
import Constants from "expo-constants";
import * as Device from "expo-device";
import { StatusBar } from "expo-status-bar";
import * as Notifications from "expo-notifications";

// The API base is taken from app.json (expo.extra.apiBase). Override with a
// MOBILE_API_BASE env var (in .env, read via extra) for a local dev backend.
const API_BASE = Constants.expoConfig?.extra?.apiBase || "https://labcare.insforge.site";

const COLORS = {
  teal: "#b91c1c",
  tealDark: "#115e59",
  ink: "#111827",
  inkSoft: "#6b7280",
  bg: "#f3f5f9",
  card: "#ffffff",
  danger: "#dc2626",
  border: "#e5e7eb",
};

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: true,
    shouldSetBadge: true,
    // A custom in-app bundle sound; falls back to the system default if the
    // file isn't bundled (e.g. running in Expo Go).
    shouldShowBanner: true,
    shouldShowList: true,
  }),
});

async function api(path, { method = "GET", body, token } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = "Bearer " + token;
  const res = await fetch(API_BASE + path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) {
    throw new Error(
      (typeof data.error === "string" && data.error) || "Request failed (" + res.status + ")"
    );
  }
  return data;
}

export default function App() {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [alerts, setAlerts] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const tokenRef = useRef(null);

  useEffect(() => {
    (async () => {
      try {
        const reg = await Notifications.getPermissionsAsync();
        const granted = reg.status === "granted" || reg.ios?.status === "AUTHORIZED";
        setAlerts(granted);
        // Try to restore a session from secure storage-less token (re-login
        // required each cold start — intentionally simple for this scaffold).
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  async function registerForPush(token) {
    if (!Device.isDevice) return null;
    try {
      const existing = await Notifications.getExpoPushTokenAsync();
      return existing.data;
    } catch (e) {
      console.warn("expo push token failed", e);
      return null;
    }
  }

  async function signIn() {
    setPending(true);
    setError("");
    try {
      const data = await api("/api/login", { method: "POST", body: { email, password } });
      tokenRef.current = data.token;
      setUser(data.user);
      // Fire-and-forget: enable phone alerts straight after login.
      await enableAlerts(data.token);
    } catch (e) {
      setError(e.message || "Sign-in failed");
    } finally {
      setPending(false);
    }
  }

  async function enableAlerts(token) {
    try {
      const settings = await Notifications.requestPermissionsAsync();
      const granted = settings.status === "granted" || settings.ios?.status === "AUTHORIZED";
      setAlerts(granted);
      if (!granted) return;

      const pushToken = await registerForPush(token);
      await api("/api/app/register", {
        method: "POST",
        token,
        body: {
          push_token: pushToken,
          platform: Platform.OS === "ios" ? "ios" : "android",
          alert_on: true,
          device_name: Device.deviceName || Device.modelName || "phone",
        },
      });
    } catch (e) {
      console.warn("enableAlerts", e);
    }
  }

  async function toggleAlerts(value) {
    setAlerts(value);
    const token = tokenRef.current;
    if (!token || !user) return;
    try {
      if (value) {
        await enableAlerts(token);
      } else {
        const t = await registerForPush(token);
        if (t) await api("/api/app/unregister", { method: "POST", token, body: { push_token: t } });
      }
    } catch (e) {
      console.warn("toggleAlerts", e);
    }
  }

  async function signOut() {
    const token = tokenRef.current;
    try {
      const t = await registerForPush(token);
      if (t) await api("/api/app/unregister", { method: "POST", token, body: { push_token: t } });
    } catch (e) {}
    tokenRef.current = null;
    setUser(null);
    setEmail("");
    setPassword("");
  }

  if (loading) {
    return (
      <View style={styles.center}>
        <ActivityIndicator color={COLORS.teal} size="large" />
        <Text style={styles.muted}>Loading LabSynch…</Text>
      </View>
    );
  }

  return (
    <SafeAreaView style={styles.safe}>
      <StatusBar style="light" />
      {user ? (
        <ScrollView contentContainerStyle={styles.container}>
          <View style={styles.header}>
            <Text style={styles.appName}>LabSynch</Text>
            <Text style={styles.appTag}>Equipment complaints &amp; breakdowns</Text>
          </View>

          <View style={styles.card}>
            <Text style={styles.hello}>Hello, {user.name}</Text>
            <Text style={styles.muted}>{user.email}</Text>
            <Text style={styles.role}>{roleLabel(user.role)}</Text>
          </View>

          <View style={styles.card}>
            <View style={styles.row}>
              <View style={{ flex: 1 }}>
                <Text style={styles.rowTitle}>Phone alerts</Text>
                <Text style={styles.rowSub}>
                  Ring on this phone even when the app is closed or the phone is locked.
                </Text>
              </View>
              <Switch
                value={alerts}
                onValueChange={toggleAlerts}
                trackColor={{ false: "#d1d5db", true: COLORS.teal }}
                thumbColor="#fff"
              />
            </View>
          </View>

          <View style={styles.card}>
            <View style={styles.row}>
              <View style={{ flex: 1 }}>
                <Text style={styles.rowTitle}>Home</Text>
                <Text style={styles.rowSub}>
                  This device is linked to your LabSynch account
                  {Device.isDevice ? "" : " (Emulator/Expo Go: push may not arrive)"}.
                </Text>
              </View>
              <Text style={{ color: COLORS.teal, fontSize: 22 }}>🔔</Text>
            </View>
          </View>

          <TouchableOpacity style={styles.outlineBtn} onPress={signOut}>
            <Text style={styles.outlineText}>Sign out</Text>
          </TouchableOpacity>
          <Text style={styles.footnote}>
            Sign in on the web at {API_BASE.replace("https://", "")} to see tickets
            and manage your team.
          </Text>
        </ScrollView>
      ) : (
        <ScrollView contentContainerStyle={[styles.container, { justifyContent: "center" }]}>
          <View style={styles.header}>
            <Text style={styles.appName}>LabSynch</Text>
            <Text style={styles.appTag}>Sign in with your LabSynch account to get phone alerts</Text>
          </View>

          <View style={styles.card}>
            {!!error && <Text style={styles.error}>{error}</Text>}
            <Text style={styles.label}>Email</Text>
            <TextInput
              style={styles.input}
              value={email}
              onChangeText={setEmail}
              autoCapitalize="none"
              keyboardType="email-address"
              autoComplete="email"
              placeholder="you@example.com"
              placeholderTextColor="#9ca3af"
            />
            <Text style={styles.label}>Password</Text>
            <TextInput
              style={styles.input}
              value={password}
              onChangeText={setPassword}
              secureTextEntry
              placeholder="••••••••"
              placeholderTextColor="#9ca3af"
            />
            <TouchableOpacity style={styles.primaryBtn} onPress={signIn} disabled={pending}>
              {pending ? (
                <ActivityIndicator color="#fff" />
              ) : (
                <Text style={styles.primaryText}>Sign in &amp; enable phone alerts</Text>
              )}
            </TouchableOpacity>
          </View>

          <Text style={styles.footnote}>
            Push works with the LabSynch deployed backend via Firebase Cloud Messaging.
            See mobile/README.md for the one-time setup.
          </Text>
        </ScrollView>
      )}
    </SafeAreaView>
  );
}

function roleLabel(role) {
  return { admin: "Administrator", engineer: "Engineer", application: "Application", customer: "Customer" }[role] || role;
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.bg },
  center: { flex: 1, alignItems: "center", justifyContent: "center", backgroundColor: COLORS.bg },
  container: { padding: 20, flexGrow: 1 },
  header: { alignItems: "center", marginBottom: 20, marginTop: 8 },
  appName: { fontSize: 34, fontWeight: "800", color: COLORS.teal, letterSpacing: 0.5 },
  appTag: { fontSize: 13, color: COLORS.inkSoft, marginTop: 4, textAlign: "center" },
  card: {
    backgroundColor: COLORS.card,
    borderRadius: 16,
    padding: 18,
    marginBottom: 14,
    borderWidth: 1,
    borderColor: COLORS.border,
  },
  hello: { fontSize: 18, fontWeight: "700", color: COLORS.ink },
  role: { fontSize: 13, color: COLORS.teal, marginTop: 4, fontWeight: "600" },
  muted: { fontSize: 14, color: COLORS.inkSoft, marginTop: 2 },
  row: { flexDirection: "row", alignItems: "center" },
  rowTitle: { fontSize: 16, fontWeight: "700", color: COLORS.ink },
  rowSub: { fontSize: 13, color: COLORS.inkSoft, marginTop: 3, lineHeight: 18 },
  label: { fontSize: 13, color: COLORS.inkSoft, marginBottom: 6, marginTop: 12, fontWeight: "600" },
  input: {
    backgroundColor: COLORS.bg,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: COLORS.border,
    padding: 12,
    fontSize: 15,
    color: COLORS.ink,
  },
  primaryBtn: {
    backgroundColor: COLORS.teal,
    borderRadius: 12,
    paddingVertical: 14,
    alignItems: "center",
    marginTop: 18,
  },
  primaryText: { color: "#fff", fontSize: 15, fontWeight: "700" },
  outlineBtn: {
    borderWidth: 1,
    borderColor: COLORS.danger,
    borderRadius: 12,
    paddingVertical: 13,
    alignItems: "center",
    backgroundColor: COLORS.card,
  },
  outlineText: { color: COLORS.danger, fontSize: 15, fontWeight: "700" },
  error: { color: COLORS.danger, fontSize: 13, marginBottom: 6 },
  footnote: { fontSize: 12, color: COLORS.inkSoft, textAlign: "center", marginTop: 16, lineHeight: 18 },
});
