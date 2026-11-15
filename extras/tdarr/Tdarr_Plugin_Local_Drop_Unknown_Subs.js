const details = () => ({
  id: 'Tdarr_Plugin_Local_Drop_Unknown_Subs',
  Stage: 'Pre-processing',
  Name: 'Drop Unknown Subtitle Tracks',
  Type: 'Video',
  Operation: 'Transcode',
  Description: `Removes subtitle streams whose codec ffprobe/ffmpeg cannot identify
  (codec_name "unknown" or missing). Later plugins use "-map 0 -c:s copy", which makes
  ffmpeg abort with "Subtitle codec 0 is not supported / Could not write header" when such
  a track exists (seen on SPWEB WEB-DLs, Spanish "Forced" track). Stream copy only, no re-encode.`,
  Version: '1.00',
  Tags: 'pre-processing,ffmpeg,subtitle only',
  Inputs: [],
});

const plugin = (file, librarySettings, inputs, otherArguments) => {
  const lib = require('../methods/lib')();
  // eslint-disable-next-line no-param-reassign
  inputs = lib.loadDefaultValues(inputs, details);

  const response = {
    processFile: false,
    preset: '',
    container: `.${file.container}`,
    handBrakeMode: false,
    FFmpegMode: true,
    reQueueAfter: true,
    infoLog: '',
  };

  const streams = (file && file.ffProbeData && file.ffProbeData.streams) || [];
  const bad = streams.filter((s) => (
    s && s.codec_type === 'subtitle'
    && (!s.codec_name || String(s.codec_name).toLowerCase() === 'unknown')
  ));

  if (bad.length === 0) {
    response.infoLog += 'No unknown subtitle tracks. \n';
    return response;
  }

  const drops = bad.map((s) => `-map -0:${s.index}`).join(' ');
  response.processFile = true;
  response.preset = `<io> -map 0 ${drops} -c copy -max_muxing_queue_size 9999`;
  response.infoLog += `Removing ${bad.length} unknown subtitle track(s): `
    + `${bad.map((s) => `#${s.index}${s.tags && s.tags.language ? ` (${s.tags.language})` : ''}`).join(', ')} \n`;
  return response;
};

module.exports.details = details;
module.exports.plugin = plugin;
