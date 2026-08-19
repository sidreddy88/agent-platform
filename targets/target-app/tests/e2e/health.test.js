jest.mock('../../routes/helper/proxyCheckIOHelper', () => ({
  addIPToWhitelist: jest.fn().mockResolvedValue({ success: true }),
  removeIPFromWhitelist: jest.fn().mockResolvedValue({}),
}));

const request = require('supertest');
const express = require('express');
const healthCheckRouter = require('../../routes/api/healthCheck');

const app = express();
app.use('/api/healthcheck', healthCheckRouter);

describe('GET /api/healthcheck', () => {
  it('returns 200 with healthy message', async () => {
    const res = await request(app).get('/api/healthcheck/dmj8DF8M659cRHVehz97');
    expect(res.status).toBe(200);
    expect(res.body.message).toBe('Server is healthy');
  });
});
