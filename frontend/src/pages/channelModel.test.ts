import assert from 'node:assert/strict'
import test from 'node:test'
import {
  SOCIAL_CHANNELS,
  channelLabel,
  contentChannel,
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

test('derives and filters the active intended channel without inventing a new provider state', () => {
  const versions = [
    { active: false, intended_channel: 'pinterest', kind: 'ORIGINAL' },
    { active: true, intended_channel: 'linkedin', kind: 'CONTENT' },
  ]
  assert.equal(contentChannel(versions), 'linkedin')
  assert.equal(matchesChannel(versions, 'linkedin'), true)
  assert.equal(matchesChannel(versions, 'instagram'), false)
  assert.equal(matchesChannel(versions, 'all'), true)
})
