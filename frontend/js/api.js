/* SATVIQ — central API layer.
 *
 * Every backend call in the UI goes through this module. Views never
 * call fetch/XMLHttpRequest directly. The backend is same-origin by
 * default; set window.SATVIQ_API_BASE before this script loads to
 * point the UI at another origin in a split deployment.
 */
(function () {
  "use strict";

  var BASE = (window.SATVIQ_API_BASE || "").replace(/\/$/, "");

  function ApiError(message, status) {
    this.name = "ApiError";
    this.message = message;
    this.status = status || null;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function url(path) {
    return BASE + path;
  }

  /* fetch with a consistent error shape. There is NO request
   * timeout: slow backend work — extraction, Muse generation —
   * is waited out, never aborted by a timer. An AbortSignal is
   * accepted only so the Generate AI Output button can implement
   * "stop waiting" (a user action, never a timeout); the backend
   * generation itself continues and saves its result. */
  function request(path, options) {
    options = options || {};
    var init = {
      method: options.method || "GET",
      headers: options.headers || {},
      body: options.body,
    };
    if (options.signal) init.signal = options.signal;
    if (options.json !== undefined) {
      init.headers = Object.assign({}, init.headers, {
        "Content-Type": "application/json",
      });
      init.body = JSON.stringify(options.json);
    }
    return fetch(url(path), init)
      .then(function (response) {
        if (!response.ok) {
          return response
            .json()
            .catch(function () { return {}; })
            .then(function (body) {
              throw new ApiError(
                body.detail || "Request failed (" + response.status + ").",
                response.status
              );
            });
        }
        return response.json();
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") throw err; /* stop-waiting */
        if (err instanceof ApiError) throw err;
        throw new ApiError("Could not reach the backend. Is the server running?");
      });
  }

  /* Multipart upload with real upload progress (fetch cannot report
   * upload progress, so this one call uses XMLHttpRequest). */
  function processResumes(files, onProgress) {
    return new Promise(function (resolve, reject) {
      var form = new FormData();
      files.forEach(function (file) { form.append("files", file); });
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url("/api/resumes/process"));
      xhr.timeout = 0; /* 0 = no timeout (timeouts have been removed) */
      if (xhr.upload && typeof onProgress === "function") {
        xhr.upload.onprogress = function (event) {
          if (event.lengthComputable) {
            onProgress(Math.round((event.loaded / event.total) * 100));
          }
        };
      }
      xhr.onload = function () {
        var body = {};
        try { body = JSON.parse(xhr.responseText || "{}"); } catch (e) { /* keep {} */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body);
        else reject(new ApiError(body.detail || "Upload failed (" + xhr.status + ").", xhr.status));
      };
      xhr.onerror = function () {
        reject(new ApiError("Could not reach the backend. Is the server running?"));
      };
      xhr.send(form);
    });
  }

  /* Start a batch job: same multipart upload (with real upload
   * progress), but the server returns a job_id immediately and
   * processes the files in the background. Poll getBatchJob for
   * real server-side progress — processed / remaining counts,
   * the current file, and each file's result as it finishes. */
  function processResumesAsync(files, onProgress) {
    return new Promise(function (resolve, reject) {
      var form = new FormData();
      files.forEach(function (file) { form.append("files", file); });
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url("/api/resumes/process-async"));
      xhr.timeout = 0; /* 0 = no timeout (timeouts have been removed) */
      if (xhr.upload && typeof onProgress === "function") {
        xhr.upload.onprogress = function (event) {
          if (event.lengthComputable) {
            onProgress(Math.round((event.loaded / event.total) * 100));
          }
        };
      }
      xhr.onload = function () {
        var body = {};
        try { body = JSON.parse(xhr.responseText || "{}"); } catch (e) { /* keep {} */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body);
        else reject(new ApiError(body.detail || "Upload failed (" + xhr.status + ").", xhr.status));
      };
      xhr.onerror = function () {
        reject(new ApiError("Could not reach the backend. Is the server running?"));
      };
      xhr.send(form);
    });
  }

  window.Api = {
    ApiError: ApiError,
    url: url,
    health: function () { return request("/api/health"); },
    stats: function () { return request("/api/stats"); },
    getAnalytics: function (params) {
      var query = new URLSearchParams();
      Object.keys(params || {}).forEach(function (key) {
        if (params[key] !== undefined && params[key] !== null && params[key] !== "") {
          query.set(key, params[key]);
        }
      });
      var suffix = query.toString() ? "?" + query.toString() : "";
      return request("/api/analytics/summary" + suffix);
    },
    getResumeCosts: function (resumeId) {
      return request("/api/resumes/" + encodeURIComponent(resumeId) + "/costs");
    },
    processResumes: processResumes,
    processResumesAsync: processResumesAsync,
    getBatchJob: function (jobId) {
      return request("/api/resumes/jobs/" + encodeURIComponent(jobId));
    },
    listResumes: function (params) {
      var query = new URLSearchParams();
      Object.keys(params || {}).forEach(function (key) {
        if (params[key] !== undefined && params[key] !== null && params[key] !== "") {
          query.set(key, params[key]);
        }
      });
      var suffix = query.toString() ? "?" + query.toString() : "";
      return request("/api/resumes" + suffix);
    },
    getResume: function (id) {
      return request("/api/resumes/" + encodeURIComponent(id));
    },
    deleteResume: function (id) {
      return request("/api/resumes/" + encodeURIComponent(id), { method: "DELETE" });
    },
    deleteAllResumes: function () {
      return request("/api/resumes", { method: "DELETE" });
    },
    reprocessResume: function (id) {
      return request("/api/resumes/" + encodeURIComponent(id) + "/reprocess", {
        method: "POST",
      });
    },
    generateAiOutput: function (id, signal) {
      return request("/api/resumes/" + encodeURIComponent(id) + "/ai/generate", {
        method: "POST",
        signal: signal,
      });
    },
    analyzeResume: function (id) {
      /* No timeout (as everywhere): the evaluation waits for Muse
         however long the response takes. */
      return request("/api/resumes/" + encodeURIComponent(id) + "/analyze", {
        method: "POST",
      });
    },
    getAnalysis: function (id) {
      return request("/api/resumes/" + encodeURIComponent(id) + "/analysis");
    },
    getAnalysisHistory: function (id) {
      return request("/api/resumes/" + encodeURIComponent(id) + "/analysis/history");
    },
    getEvaluationConfig: function () {
      return request("/api/evaluation/config");
    },
    extractorStatus: function () {
      return request("/api/extractors/status");
    },
    testExtractors: function () {
      /* No timeout (as everywhere): Docling's first-ever run can
         legitimately take minutes. */
      return request("/api/extractors/test", { method: "POST" });
    },
    runExtractors: function (id, extractors) {
      return request("/api/resumes/" + encodeURIComponent(id) + "/extractors/run", {
        method: "POST",
        json: { extractors: extractors },
      });
    },
    downloadOriginalUrl: function (id) {
      return url("/api/resumes/" + encodeURIComponent(id) + "/file");
    },
    downloadTextUrl: function (id) {
      return url("/api/resumes/" + encodeURIComponent(id) + "/text");
    },
    getSystemPrompt: function () { return request("/api/settings/system-prompt"); },
    saveSystemPrompt: function (text) {
      return request("/api/settings/system-prompt", {
        method: "PUT",
        json: { system_prompt: text },
      });
    },
    resetSystemPrompt: function () {
      return request("/api/settings/system-prompt/reset", { method: "POST" });
    },
    testSystemPrompt: function (text) {
      return request("/api/settings/system-prompt/test", {
        method: "POST",
        json: text === undefined ? {} : { system_prompt: text },
      });
    },
  };
})();
