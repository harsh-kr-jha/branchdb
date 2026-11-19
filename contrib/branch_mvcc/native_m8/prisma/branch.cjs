const assert=require('node:assert/strict');
const {PrismaClient}=require('@prisma/client');
const prisma=new PrismaClient();
(async()=>{
 const user=await prisma.user.findUnique({where:{email:'seed@example.test'},include:{projects:{include:{tasks:{include:{comments:true}}}}}});
 assert.equal(user.name,'Inherited');assert.equal(user.projects[0].tasks[0].comments[0].body,'Shared comment');
 await prisma.user.update({where:{id:user.id},data:{name:process.env.BRANCH_NAME}});
 await assert.rejects(prisma.task.create({data:{title:'bad',projectId:'00000000-0000-0000-0000-000000000000'}}),error=>error.code==='P2003');
 await prisma.user.delete({where:{id:user.id}});
 assert.equal(await prisma.project.count(),0);assert.equal(await prisma.task.count(),0);assert.equal(await prisma.comment.count(),0);
 console.log('PASS: inherited Prisma rows, branch update, FK rejection and isolated cascade');
})().finally(()=>prisma.$disconnect()).catch(error=>{console.error(error);process.exitCode=1});
