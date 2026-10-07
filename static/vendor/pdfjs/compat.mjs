/* PDF.js 6 legacy includes collection polyfills, but still requires this
 * Promise API (missing on older Safari). Load in both browser and worker. */
if (typeof Promise.withResolvers !== 'function') {
  Promise.withResolvers = function () {
    let resolve;
    let reject;
    const promise = new this((res, rej) => { resolve = res; reject = rej; });
    return {promise, resolve, reject};
  };
}
