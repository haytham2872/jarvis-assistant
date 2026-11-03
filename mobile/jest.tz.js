// The app shows times in the phone's own zone. Its tests were written in Europe/Paris,
// which has real clock changes, so the test run is pinned to it (workers inherit TZ).
module.exports = () => {
  process.env.TZ = 'Europe/Paris';
};
