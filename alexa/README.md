# Voice cue setup

This chain makes a living room Echo speak when Acacia is confirmed on the couch. Stage 1 works on real Alexa+ and AWS: a confirmed ON fires an Alexa routine, which can play a built-in sound or say a phrase in Alexa's voice. Stage 2 plays a recording of Zimu's own voice through a private custom skill, and it works only on accounts that use the original Alexa experience, because Alexa+ does not open custom skills that Amazon has not approved. The household uses Alexa+, so the routine currently says "Acacia, get off." in Alexa's voice.

```
Mac (confirmed ON, gates pass) --signed HTTPS--> Lambda --ChangeReport--> virtual sensor
virtual sensor --> Alexa routine --> Alexa says the phrase on the chosen Echo (stage 1)
                                 --> or opens the skill, which plays the clip (stage 2, original Alexa only)
```

Everything is in `us-east-1`, which is the Alexa region for North America, even though the AWS console may open in another region.

## Stage 1: does the sensor fire a routine?

This stage plays only a built-in Alexa sound. If it fails, stop here, because the rest depends on it.

**Login with Amazon.** In the developer console, create a Security Profile, and note its client ID and secret.

**Sensor skill.** Create a skill named Acacia Sensor, using the Smart Home model. Note the skill ID. Under Permissions, turn on Send Alexa Events and note the Alexa client ID and secret shown there. Under Account Linking, use `https://www.amazon.com/ap/oa` as the authorization URI, `https://api.amazon.com/auth/o2/token` as the token URI, the Security Profile's client ID and secret, and `profile` as the scope. Add the redirect URLs shown on that page to the Security Profile's allowed return URLs.

**AWS.** Deploy the stack and upload the function code. This creates a DynamoDB table, a private S3 bucket, an IAM role, and the function with its URL, all in `us-east-1`.

```bash
./alexa/deploy.sh
```

Paste the `FunctionArn` it prints into the skill's Smart Home endpoint, then run it again with the skill ID so Alexa is allowed to call the function:

```bash
./alexa/deploy.sh amzn1.ask.skill.YOUR-SKILL-ID
```

**Secrets.** Secrets never pass through chat. The trigger secret goes from the Mac straight to AWS without being displayed; this needs a current `aws login`.

```bash
./acacia cue new-secret
```

The skill's Permissions page shows an Alexa client ID and secret under Alexa Skill Messaging once Send Alexa Events is on. This command asks for both at hidden prompts and stores them as SecureString parameters `/acacia/lwa_client_id` and `/acacia/lwa_client_secret`:

```bash
./acacia cue alexa-keys
```

**Alexa app.** Enable Acacia Sensor under your skills, link the account, and let it discover devices. A device called Acacia Couch should appear. Create a routine: when Acacia Couch detects motion, play a built-in sound on the living room Echo.

**Mac.** Set these in `config.toml`, using the `TriggerEndpoint` output, and restart.

```toml
[cue]
enabled = true
endpoint = "https://YOUR-FUNCTION-URL.lambda-url.us-east-1.on.aws/"
```

```bash
./acacia cue check
```

It should report `linked: True`. This test makes a sound in your home:

```bash
./acacia cue test
```

Note how long the sound takes, then reset the sensor after at least 35 seconds:

```bash
./acacia cue reset
```

If nothing plays, look at Amazon's Smart Home Debugger and the function's CloudWatch logs. Forum reports say a routine only fires from a proactive ChangeReport, so a sensor that looks correct in the app is not proof.

## Stage 2: play the voice clip

**Voice skill.** Create a second skill named Acacia Cue: experience Other, model Custom, hosting Provision your own, template Start from Scratch. Set its invocation name to `acacia cue`, point its Default Region endpoint at the same `FunctionArn`, build the model, and set testing to Development on the Test tab. Then allow Alexa to call the function from this skill. Arguments you leave empty keep their current values, so the sensor skill ID is kept:

```bash
./alexa/deploy.sh "" amzn1.ask.skill.YOUR-VOICE-SKILL-ID
```

Saying "Alexa, open acacia cue" should now answer "The Acacia voice clip is not set up yet."

**Clip.** Record a short clip, for example in Voice Memos, and copy it to the Mac. This converts it to the format Alexa requires (MP3 at 48 kbps, 24 kHz, mono), trims silence at both ends, evens out the loudness, uploads it to the private bucket, and points the function at it:

```bash
./alexa/upload-clip.sh ~/Downloads/recording.m4a
```

Run it again whenever you record a new clip. Each upload gets its own name, so an Echo never plays a cached older version.

**Routine.** Change the routine's action from the built-in sound to opening Acacia Cue on the living room Echo, either through a Skills action or a custom action that says "open acacia cue". As of November 2026 the developer console's simulator refused en-US with "Unsupported locale", so test on a real Echo instead.

This stage needs an account on the original Alexa experience. Alexa+ answers "not supported on this device" to any custom skill that Amazon has not approved for direct invocation, and the skill's page in the Alexa app then says "Language support is limited for Alexa+ customers". Stage 1 works under Alexa+.

## Using it

Run disarmed for a few days first; see `docs/VALIDATION.md`. Arm it in the Cue tab, and only while someone is home at first. It plays at most once per visit, and it stays silent during quiet hours, during the cooldown, past the daily limit, and when people were seen recently. These are set in `[cue]`. The Lambda also refuses triggers less than 60 seconds apart, whatever the Mac asks. Every start is disarmed, so nothing plays after an unattended restart.

The Function URL is public by design, because the Mac has no AWS identity. Requests need a valid signature and a timestamp under 60 seconds old, and the worst an attacker holding the secret could do is play the clip. If the secret leaks, run `./acacia cue new-secret`, which replaces it in both places.

## Removing it

Set `cue.enabled = false`, delete the `acacia-cue` stack (empty the bucket first), delete the three parameters, and delete both skills and the Security Profile in the developer console.
