import assert from 'node:assert/strict'
import test from 'node:test'
import {
  SOCIAL_CHANNELS,
  channelLabel,
  contentChannel,
  contentChannels,
  generationChannel,
  matchesChannel,
  normalizeSocialChannel,
} from '../ui/channelModel.ts'

test('exposes the six first-class social channels in product order', () => {
  assert.deepEqual(SOCIAL_CHANNELS.map((channel) => channel.key), [
    'pinterest', 'instagram', 'facebook', 'linkedin', 'tiktok', 'youtube',
  ])
})

test('normalizes legacy YouTube Shorts revisions to the YouTube UI identity', () => {
  assert.equal(normalizeSocialChannel('youtube_shorts'), 'youtube')
  assert.equal(generationChannel('youtube'), 'youtube_shorts')
  assert.equal(channelLabel('youtube_shorts'), 'YouTube')
})

test('derives the active intended channel and indexes every persisted channel variant', () => {
  const versions = [
    { active: false, intended_channel: 'pinterest', kind: 'ORIGINAL' },
    { active: true, intended_channel: 'linkedin', kind: 'CONTENT' },
    { active: false, intended_channel: 'youtube_shorts', kind: 'VIDEO_SPEC' },
  ]
  assert.equal(contentChannel(versions), 'linkedin')
  assert.deepEqual(contentChannels(versions), ['pinterest', 'linkedin', 'youtube'])
  assert.equal(matchesChannel(versions, 'linkedin'), true)
  assert.equal(matchesChannel(versions, 'youtube'), true)
  assert.equal(matchesChannel(versions, 'instagram'), false)
  assert.equal(matchesChannel(versions, 'all'), true)
})
