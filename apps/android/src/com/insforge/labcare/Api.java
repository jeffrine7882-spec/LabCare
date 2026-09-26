package com.insforge.labcare;

import org.json.JSONObject;

/** Tiny JSON HTTP helper shared by all screens. */
final class Api {

    static final String BASE = "https://labcare.insforge.site";

    static String get(String path, String token) throws Exception {
        java.net.URL url = new java.net.URL(BASE + path);
        java.net.HttpURLConnection c = (java.net.HttpURLConnection) url.openConnection();
        c.setRequestMethod("GET");
        c.setConnectTimeout(10000);
        c.setReadTimeout(20000);
        c.setRequestProperty("Accept", "application/json");
        if (token != null && !token.isEmpty()) {
            c.setRequestProperty("Authorization", "Bearer " + token);
        }
        return readStream(c);
    }

    static String post(String path, String jsonBody, String token) throws Exception {
        java.net.URL url = new java.net.URL(BASE + path);
        java.net.HttpURLConnection c = (java.net.HttpURLConnection) url.openConnection();
        c.setRequestMethod("POST");
        c.setConnectTimeout(10000);
        c.setReadTimeout(20000);
        c.setRequestProperty("Content-Type", "application/json");
        c.setRequestProperty("Accept", "application/json");
        if (token != null && !token.isEmpty()) {
            c.setRequestProperty("Authorization", "Bearer " + token);
        }
        c.setDoOutput(true);
        c.getOutputStream().write(jsonBody.getBytes("UTF-8"));
        return readStream(c);
    }

    /** An HTTP answer with enough context to tell the API's own verdicts apart. */
    static final class Response {
        final int code;
        final String contentType;
        final String body;

        Response(int code, String contentType, String body) {
            this.code = code;
            this.contentType = contentType == null ? "" : contentType;
            this.body = body == null ? "" : body;
        }

        /**
         * True only for a 401 that carries the LabSynch API's own JSON error
         * body. A proxy, captive portal or half-started server answers 401
         * too — with HTML or an empty body — and those must never count as
         * "your session is gone".
         */
        boolean isApiAuthFailure() {
            if (code != 401 || !contentType.toLowerCase().contains("application/json")) return false;
            try {
                return new JSONObject(body).optString("error", "").length() > 0;
            } catch (Exception e) {
                return false;
            }
        }
    }

    /** GET with status + content type (the relay's session checks need them). */
    static Response fetch(String path, String token) throws Exception {
        java.net.URL url = new java.net.URL(BASE + path);
        java.net.HttpURLConnection c = (java.net.HttpURLConnection) url.openConnection();
        c.setRequestMethod("GET");
        c.setConnectTimeout(10000);
        c.setReadTimeout(20000);
        c.setRequestProperty("Accept", "application/json");
        if (token != null && !token.isEmpty()) {
            c.setRequestProperty("Authorization", "Bearer " + token);
        }
        String body = readStream(c);
        return new Response(c.getResponseCode(), c.getContentType(), body);
    }

    static String readStream(java.net.HttpURLConnection c) throws Exception {
        int code = c.getResponseCode();
        java.io.InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
        if (in == null) return "";
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        return new String(out.toByteArray(), "UTF-8");
    }
}
