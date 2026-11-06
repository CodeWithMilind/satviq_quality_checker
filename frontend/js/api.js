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
  var DEFAULT_TIMEOUT_MS = 30000;
  var PROCESS_TIMEOUT_MS = 600000;

  function ApiError(message, status) {
    this.name = "ApiError";
    this.message = message;
    this.status = status || null;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function url(path) {
    return BASE + path;
  }

  /* fetch with an abort-based timeout and consistent error shape. */
  function request(path, options) {
    options = options || {};
    var timeout = options.timeout || DEFAULT_TIMEOUT_MS;
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, timeout);
    var init = {
      method: options.method || "GET",
      headers: options.headers || {},
      body: options.body,
      signal: controller.signal,
    };
    if (options.json !== undefined) {
      init.headers = Object.assign({}, init.headers, {
        "Content-Type": "application/json",
      });
      init.body = JSON.stringify(options.json);
    }
    return fetch(url(path), init)
      .then(function (response) {
        clearTimeout(timer);
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
        clearTimeout(timer);
        if (err.name === "AbortError") {
          throw new ApiError("The request timed out. Please try again.");
        }
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
      xhr.timeout = PROCESS_TIMEOUT_MS;
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
      xhr.ontimeout = function () {
        reject(new ApiError("Processing timed out. Please try again."));
      };
      xhr.send(form);
    });
  }

  window.Api = {
    ApiError: ApiError,
    url: url,
    health: function () { return request("/api/health"); },
    stats: function () { return request("/api/stats"); },
    processResumes: processResumes,
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
    runExtractors: function (id, extractors) {
      return request("/api/resumes/" + encodeURIComponent(id) + "/extractors/run", {
        method: "POST",
        json: { extractors: extractors },
        timeout: 600000,
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
        timeout: 120000,
      });
    },
  };
})();
