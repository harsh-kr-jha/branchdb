const assert = require('node:assert/strict');
const { PrismaClient } = require('@prisma/client');
const prisma = new PrismaClient();
(async () => {
  const name = process.env.BRANCH_NAME;
  const user = await prisma.user.create({data:{email:'parallel@example.test',name}});
  await prisma.project.create({data:{name,ownerId:user.id}});
  await new Promise(resolve => setTimeout(resolve,150));
  assert.equal((await prisma.user.findUnique({where:{email:user.email}})).name,name);
  console.log('PASS: parallel Prisma application on branch '+name);
})().finally(()=>prisma.$disconnect()).catch(error=>{console.error(error);process.exitCode=1});
