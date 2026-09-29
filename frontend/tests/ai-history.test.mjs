import test from 'node:test'
import assert from 'node:assert/strict'
import {HISTORY_KEY,readHistory,writeHistory,snapshotMessages} from '../src/aiHistory.js'
const storage=()=>{const data=new Map();return {getItem:key=>data.get(key),setItem:(key,value)=>data.set(key,value)}}
test('round trip restores conversation and disables old actions',()=>{const s=storage();writeHistory(s,[{id:'one',title:'测试',updatedAt:1,messages:[{role:'assistant',content:'方案',action:{operation:'create'},executing:true}]}],'one');const saved=readHistory(s);assert.equal(saved.activeId,'one');assert.equal(saved.sessions[0].messages[0].restored,true);assert.equal(saved.sessions[0].messages[0].executing,undefined)})
test('keeps latest 20 sessions and 100 messages',()=>{const s=storage();const sessions=Array.from({length:25},(_,i)=>({id:String(i),title:'会话',updatedAt:i,messages:Array.from({length:110},()=>({role:'user',content:'test'}))}));writeHistory(s,sessions,'24');const saved=readHistory(s);assert.equal(saved.sessions.length,20);assert.equal(saved.sessions[0].id,'24');assert.equal(saved.sessions[0].messages.length,100)})
test('deletion persists and empty active session stays empty',()=>{const s=storage();writeHistory(s,[],'new');assert.deepEqual(readHistory(s),{activeId:'new',sessions:[]})})
test('corrupt storage and quota errors are surfaced',()=>{const s=storage();s.setItem(HISTORY_KEY,'broken');assert.throws(()=>readHistory(s));assert.throws(()=>writeHistory({setItem(){throw Error('quota')}},[],'new'))})
test('snapshot never mutates live messages',()=>{const live=[{role:'assistant',content:'test',executing:true}];snapshotMessages(live);assert.equal(live[0].executing,true);assert.equal(live[0].restored,undefined)})
