import assert from 'node:assert/strict';
import {percent} from '../../src/investment_agent/web/static/agent_portfolio.js';
for (const [value,expected] of [[null,'未知'],['0','0%'],['0.5','50%'],['1E-18','0.0000000000000001%'],['-0.1','-10%'],['-0','0%'],['0.3333333333333333333333333333333333333333','33.3333…%'],['+1e0','100%']]) assert.equal(percent(value),expected);
assert.match(percent('1E10001'),/超出展示范围/);
console.log('AG24 exact decimal-string percentage presentation passed');
