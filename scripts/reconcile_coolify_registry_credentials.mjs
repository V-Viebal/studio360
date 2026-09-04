#!/usr/bin/env node
/** Value-free all-scope cleanup/readback for temporary GHCR rows. */
import { execFileSync } from 'node:child_process';
function required(name){const v=process.env[name]; if(!v) throw new Error(`${name} is required`); return v;}
const base=required('COOLIFY_URL').replace(/\/$/,''); const token=required('COOLIFY_API_TOKEN'); const uuid=required('COOLIFY_APPLICATION_UUID');
const host=new URL(base).hostname; const origin=process.env.COOLIFY_ORIGIN_IP;
function req(method,path,body){const args=['--fail-with-body','--silent','--show-error','--retry','5','--retry-all-errors','--retry-delay','3','--connect-timeout','15','--max-time','120','--request',method,'--header',`Authorization: Bearer ${token}`,'--header','Accept: application/json']; if(origin) args.push('--resolve',`${host}:443:${origin}`); if(body){args.push('--header','Content-Type: application/json','--data',JSON.stringify(body));} args.push(`${base}${path}`); return JSON.parse(execFileSync('curl',args,{encoding:'utf8'}));}
const app=req('GET',`/api/v1/applications/${uuid}`); for(const [key,expected] of [['COOLIFY_APPLICATION_NAME','name'],['COOLIFY_ENVIRONMENT_NAME','environment']]) {if(process.env[key] && (expected==='name'?app.name:app.environment?.name)!==process.env[key]) throw new Error(`target lock mismatch for ${expected}`);}
const keys=['GHCR_USERNAME','GHCR_TOKEN']; const before=req('GET',`/api/v1/applications/${uuid}/envs`); const rows=before.filter(x=>keys.includes(x.key)); const deleted=[]; for(const row of rows){req('DELETE',`/api/v1/applications/${uuid}/envs/${row.uuid}`); deleted.push({uuid:row.uuid,key:row.key});}
const after=req('GET',`/api/v1/applications/${uuid}/envs`); const remaining=after.filter(x=>keys.includes(x.key)); console.log(JSON.stringify({operation:'delete-and-readback',application_uuid:uuid,before_count:rows.length,deleted,after_count:remaining.length,verified_absent:remaining.length===0},null,2)); if(remaining.length) process.exit(1);
