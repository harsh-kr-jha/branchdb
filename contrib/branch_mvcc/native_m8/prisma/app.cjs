const assert = require('node:assert/strict');
const { PrismaClient } = require('@prisma/client');
const prisma = new PrismaClient();
(async () => {
  const user = await prisma.user.create({data:{email:'harsh@example.test',name:'Harsh',projects:{create:{name:'Build',tasks:{create:{title:'Ship',comments:{create:{body:'First'}}}}}}},include:{projects:{include:{tasks:{include:{comments:true}}}}}});
  assert.equal(user.projects[0].tasks[0].status,'todo');
  assert.equal(user.projects[0].tasks[0].comments[0].body,'First');
  assert.ok(user.createdAt instanceof Date);
  await prisma.user.upsert({where:{email:user.email},create:{email:user.email,name:'wrong'},update:{name:'Updated'}});
  assert.equal((await prisma.user.findUnique({where:{email:user.email}})).name,'Updated');
  await assert.rejects(prisma.project.create({data:{name:'Orphan',ownerId:'00000000-0000-0000-0000-000000000000'}}), error => error.code === 'P2003');
  await prisma.user.delete({where:{id:user.id}});
  assert.equal(await prisma.project.count(),0);
  assert.equal(await prisma.task.count(),0);
  assert.equal(await prisma.comment.count(),0);
  console.log('PASS: actual Prisma nested CRUD, defaults, upsert, FK rejection and cascade');
})().finally(()=>prisma.$disconnect()).catch(error=>{console.error(error);process.exitCode=1});
