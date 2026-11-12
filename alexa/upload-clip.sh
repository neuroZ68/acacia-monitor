#!/bin/sh
# Converts a voice recording to the format Alexa plays, uploads it to the private
# clip bucket, and points the function at it.
# Usage: ./alexa/upload-clip.sh recording.m4a   (any format ffmpeg reads)
#
# Silence at both ends is trimmed and pauses longer than a third of a second inside
# the clip are shortened to about a quarter second, because dead air delays the cue.
# Every word is kept. Loudness is normalized so the clip is neither lost nor
# startling. Alexa requires MP3 at 48 kbps with a 16, 22.05, or 24 kHz sample rate.
# Needs a current `aws login`.
set -eu
cd "$(dirname "$0")/.."
if [ $# -ne 1 ] || [ ! -f "$1" ]; then
    echo "Usage: $0 recording-file" >&2
    exit 2
fi
export AWS_REGION=us-east-1 AWS_PAGER=""

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Sounds shorter than 0.15 s at either end, such as the tap that stops a recording,
# count as silence. Speech sits far above -40 dB, a quiet room well below it.
front="start_periods=1:start_threshold=-40dB:start_duration=0.15:start_silence=0.05"
pauses="stop_periods=-1:stop_threshold=-40dB:stop_duration=0.3:stop_silence=0.25"
tail="start_periods=1:start_threshold=-40dB:start_duration=0.15:start_silence=0.2"
ffmpeg -hide_banner -loglevel error -i "$1" -vn -ac 1 \
    -af "silenceremove=$front:$pauses,areverse,silenceremove=$tail,areverse,loudnorm=I=-16:TP=-1.5:LRA=11" \
    -ar 24000 -codec:a libmp3lame -b:a 48k "$work/clip.mp3"

seconds="$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$work/clip.mp3")"
printf 'Converted clip: %.1f s\n' "$seconds"
if [ "$(echo "$seconds > 10" | bc)" -eq 1 ]; then
    echo "The clip is longer than 10 seconds. Keep it short; a late or long cue is worse than none." >&2
    exit 1
fi

bucket="$(aws cloudformation describe-stacks --stack-name acacia-cue \
    --query "Stacks[0].Outputs[?OutputKey=='ClipBucket'].OutputValue" --output text)"
key="clips/voice-$(shasum -a 256 "$work/clip.mp3" | cut -c1-12).mp3"
aws s3 cp "$work/clip.mp3" "s3://$bucket/$key" --content-type audio/mpeg --only-show-errors
echo "Uploaded to the private bucket as $key"

./alexa/deploy.sh "" "" "$key"
