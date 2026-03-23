// lib/supabaseClient.js
// ─────────────────────────────────────────────────────────────
// Cliente Supabase centralizado para el frontend
// Requiere: https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2
// Variables de entorno Vercel: SUPABASE_URL y SUPABASE_ANON_KEY
// ─────────────────────────────────────────────────────────────

import { createClient } from "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm";

// ── Configuración ────────────────────────────────────────────
// En Vercel define estas variables en Project Settings → Environment Variables
// y expónlas al frontend con el prefijo NEXT_PUBLIC_ o pásalas inline al HTML.
const SUPABASE_URL      = window.__ENV__?.SUPABASE_URL      || "";
const SUPABASE_ANON_KEY = window.__ENV__?.SUPABASE_ANON_KEY || "";

if (!SUPABASE_URL || !SUPABASE_ANON_KEY) {
  console.error(
    "[supabaseClient] Faltan variables de entorno: SUPABASE_URL y SUPABASE_ANON_KEY.\n" +
    "Asegúrate de inyectarlas en window.__ENV__ desde tu HTML o backend."
  );
}

// ── Instancia singleton ──────────────────────────────────────
export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);


// ════════════════════════════════════════════════════════════
// KOM PROFILES  (espejo de kom_profiles.py en el frontend)
// Tabla esperada en Supabase:
//   kom_profiles (person_id TEXT, chamber TEXT, notas TEXT,
//                 tags JSONB, links JSONB, updated_at TIMESTAMPTZ)
//   PK compuesta: (person_id, chamber)
// ════════════════════════════════════════════════════════════

/**
 * Obtiene el perfil KOM de una persona.
 * @param {string} chamber   - "camara" | "senado"
 * @param {string} person_id
 * @returns {Promise<{success: boolean, exists: boolean, profile: object}>}
 */
export async function getKomProfile(chamber, person_id) {
  const { data, error } = await supabase
    .from("kom_profiles")
    .select("*")
    .eq("chamber", chamber)
    .eq("person_id", person_id)
    .maybeSingle();

  if (error) {
    console.error("[getKomProfile]", error.message);
    return { success: false, exists: false, profile: null, error: error.message };
  }

  if (!data) {
    return {
      success: true,
      exists: false,
      profile: { notas: "", tags: [], links: [] },
    };
  }

  return { success: true, exists: true, profile: data };
}

/**
 * Crea o actualiza el perfil KOM de una persona (upsert).
 * @param {string} chamber
 * @param {string} person_id
 * @param {{ notas?: string, tags?: string[], links?: string[] }} payload
 * @returns {Promise<{success: boolean, saved: boolean, profile: object}>}
 */
export async function upsertKomProfile(chamber, person_id, payload) {
  const profile = {
    person_id,
    chamber,
    notas:      payload.notas      ?? "",
    tags:       payload.tags       ?? [],
    links:      payload.links      ?? [],
    updated_at: new Date().toISOString(),
  };

  const { data, error } = await supabase
    .from("kom_profiles")
    .upsert(profile, { onConflict: "person_id,chamber" })
    .select()
    .single();

  if (error) {
    console.error("[upsertKomProfile]", error.message);
    return { success: false, saved: false, profile: null, error: error.message };
  }

  return { success: true, saved: true, profile: data };
}


// ════════════════════════════════════════════════════════════
// AUTH  (opcional — descomenta si necesitas login)
// ════════════════════════════════════════════════════════════

/**
 * Login con email + password.
 */
export async function signIn(email, password) {
  const { data, error } = await supabase.auth.signInWithPassword({ email, password });
  if (error) return { success: false, error: error.message };
  return { success: true, session: data.session, user: data.user };
}

/**
 * Logout.
 */
export async function signOut() {
  const { error } = await supabase.auth.signOut();
  return error ? { success: false, error: error.message } : { success: true };
}

/**
 * Devuelve la sesión activa (null si no hay).
 */
export async function getSession() {
  const { data } = await supabase.auth.getSession();
  return data.session;
}

/**
 * Suscripción a cambios de sesión.
 * @param {(session: object|null) => void} callback
 */
export function onAuthChange(callback) {
  return supabase.auth.onAuthStateChange((_event, session) => callback(session));
}


// ════════════════════════════════════════════════════════════
// REALTIME  (opcional — escucha cambios en kom_profiles)
// ════════════════════════════════════════════════════════════

/**
 * Suscribe a cambios en tiempo real de los perfiles KOM.
 * @param {(payload: object) => void} callback
 * @returns canal (llama a canal.unsubscribe() para cerrar)
 */
export function subscribeKomProfiles(callback) {
  const canal = supabase
    .channel("kom_profiles_changes")
    .on(
      "postgres_changes",
      { event: "*", schema: "public", table: "kom_profiles" },
      (payload) => callback(payload)
    )
    .subscribe();

  return canal;
}